/*
 * Voice dictation stick — firmware for the Atech Tiny (3-port) board.
 *
 * Hold the button: audio from the mic streams to the computer over USB.
 * Release it: the stream ends, the computer transcribes and types the text.
 *
 * Hardware (pins measured on the real board; see the MIC_* comment below):
 *   Ports 1+2  Microphone   LR=GPIO17 WS=GPIO18
 *                           SCK=GPIO41 SD=GPIO40
 *   Port 3     Button       A=GPIO35 (switch) B=GPIO36 (RGB light, V2.0)
 *   Port 4     USB-C        native USB (D-/D+ on GPIO19/20)
 *
 * Wire protocol, device -> host (binary, over USB-CDC):
 *   [0xA5 0x5A] [type:1] [len:2, little endian] [payload:len]
 *     'S'  recording started            (len 0)
 *     'D'  audio, int16 LE mono 16 kHz  (len <= 1024, always even)
 *     'E'  recording ended              (len 4: uint32 dropped-sample count)
 *     'A'  recording aborted — it was only a tap; discard it (len 0)
 *     'T'  single tap                   (len 0)  host: Enter if text waiting, else Tab
 *     'N'  double tap                   (len 0)  host: delete the waiting text
 *   Driver log lines ("[Button] Pressed" etc.) may appear *between* packets;
 *   the host skips anything that isn't a packet.
 *
 * Host -> device (single bytes, drive the button light):
 *   'B' busy/transcribing (amber)   'K' done (green flash)
 *   'X' error (red flash)           'I' idle (off)
 */

#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "esp_task_wdt.h"

#include "modules/microphone.h"
#include "modules/button.h"

// ---- pins (Tiny board) ------------------------------------------------------
// Measured on the real board (firmware/diag): the mic only answers with its
// "port 1" half on board port 2 and its "port 2" half on board port 1 — the
// module is seated the other way round from tiny.md's layout. If you ever
// re-seat it, rerun `pio run -e diag -t upload` and pick the live combo.
static const int MIC_LR  = 17;   // board port 2 Data A
static const int MIC_WS  = 18;   // board port 2 Data B
static const int MIC_SCK = 41;   // board port 1 Data A
static const int MIC_SD  = 40;   // board port 1 Data B
static const int BTN_SW  = 35;   // port 3 Data A
static const int BTN_LED = 36;   // port 3 Data B

// ---- tuning -----------------------------------------------------------------
static const uint32_t TAIL_MS        = 300;     // keep recording after release so the last word isn't clipped
static const uint32_t MAX_RECORD_MS  = 120000;  // hard cap on one dictation
static const uint32_t HOST_WAIT_MS   = 4000;    // amber light gives up if the host never answers
static const uint32_t TAP_MS         = 250;     // a press shorter than this is a tap, not dictation
static const uint32_t DOUBLE_TAP_MS  = 350;     // max gap from releasing tap 1 to pressing tap 2
                                                // (also how long a single tap waits before acting)
static const size_t   STREAM_BYTES   = 16384;   // ~0.5 s of audio buffered between mic task and USB
static const size_t   PACKET_BYTES   = 1024;    // audio bytes per 'D' packet

// ---- modules ----------------------------------------------------------------
Microphone   mic(MIC_LR, MIC_WS, MIC_SCK, MIC_SD);
ButtonModule button(BTN_SW, BTN_LED);

TaskHandle_t mainTaskHandle = NULL;

// ---- shared with the mic capture task (core 0) ------------------------------
static StreamBufferHandle_t audioStream = NULL;
static volatile bool        capturing    = false;
static volatile uint32_t    droppedSamples = 0;

// Runs on the mic's capture task. Must be fast and never block.
static void onMicAudio(const int16_t* samples, int count, void* /*ctx*/) {
    if (!capturing || audioStream == NULL) return;
    size_t bytes = (size_t)count * sizeof(int16_t);
    // All-or-nothing so the stream never holds half a sample.
    if (xStreamBufferSpacesAvailable(audioStream) >= bytes) {
        xStreamBufferSend(audioStream, samples, bytes, 0);
    } else {
        droppedSamples += count;
    }
}

// ---- USB output -------------------------------------------------------------
// Write everything or give up after a short wait (host not reading). A partial
// packet is harmless: the host resyncs on the next magic header.
static bool writeAll(const uint8_t* p, size_t n) {
    size_t off = 0;
    uint32_t t0 = millis();
    while (off < n) {
        size_t w = Serial.write(p + off, n - off);
        off += w;
        if (w == 0) {
            if (millis() - t0 > 50) return false;
            vTaskDelay(1);
        }
    }
    return true;
}

static bool sendPacket(char type, const uint8_t* payload, uint16_t len) {
    uint8_t hdr[5] = {0xA5, 0x5A, (uint8_t)type, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
    if (!writeAll(hdr, sizeof(hdr))) return false;
    if (len && !writeAll(payload, len)) return false;
    return true;
}

static void drainAudio() {
    static uint8_t chunk[PACKET_BYTES];
    // Bound the work per loop so the watchdog and button stay serviced.
    for (int i = 0; i < 8; i++) {
        size_t avail = xStreamBufferBytesAvailable(audioStream);
        if (avail < 2) return;
        size_t want = min(avail, PACKET_BYTES) & ~(size_t)1;   // keep it even
        size_t got = xStreamBufferReceive(audioStream, chunk, want, 0);
        if (got) sendPacket('D', chunk, (uint16_t)got);
    }
}

// ---- light ------------------------------------------------------------------
enum LightState { L_IDLE, L_RECORDING, L_BUSY, L_FLASH };
static LightState lightState = L_IDLE;
static uint32_t   lightSince = 0;

static void setLight(LightState s) {
    lightState = s;
    lightSince = millis();
    switch (s) {
        case L_IDLE:      button.off();                 break;
        case L_RECORDING: button.setColor(255, 0, 0);   break;
        case L_BUSY:      button.setColor(255, 120, 0); break;
        case L_FLASH:     /* colour set by caller */    break;
    }
}

static void flash(uint8_t r, uint8_t g, uint8_t b) {
    button.setColor(r, g, b);
    setLight(L_FLASH);
}

static void serviceLight() {
    uint32_t age = millis() - lightSince;
    if (lightState == L_FLASH && age > 350)          setLight(L_IDLE);
    if (lightState == L_BUSY  && age > HOST_WAIT_MS) setLight(L_IDLE);
}

static void readHost() {
    while (Serial.available() > 0) {
        int c = Serial.read();
        if (lightState == L_RECORDING) continue;    // recording owns the light
        switch (c) {
            case 'B': setLight(L_BUSY);   break;
            case 'K': flash(0, 255, 0);   break;
            case 'X': flash(255, 0, 0);   break;
            case 'I': setLight(L_IDLE);   break;
            default: break;
        }
    }
}

// ---- recording state machine ------------------------------------------------
static bool     recording   = false;   // between 'S' and 'E'
static bool     tailing     = false;   // released, finishing the tail
static uint32_t recStart    = 0;
static uint32_t releasedAt  = 0;
static uint32_t pressedAt   = 0;
static bool     freshPress  = false;   // this press started the current recording

static void startRecording() {
    capturing = false;
    xStreamBufferReset(audioStream);
    droppedSamples = 0;
    sendPacket('S', nullptr, 0);
    recording = true;
    tailing   = false;
    recStart  = millis();
    capturing = true;
    setLight(L_RECORDING);
}

static void finishRecording() {
    capturing = false;
    drainAudio();
    drainAudio();
    uint32_t dropped = droppedSamples;
    sendPacket('E', (const uint8_t*)&dropped, sizeof(dropped));
    recording = false;
    tailing   = false;
    setLight(L_BUSY);   // the host answers with K / X / I
}

// The whole recording was one short tap: tell the host to throw it away.
static void abortRecording() {
    capturing = false;
    xStreamBufferReset(audioStream);
    sendPacket('A', nullptr, 0);
    recording = false;
    tailing   = false;
    setLight(L_IDLE);
}

// ---- taps -------------------------------------------------------------------
static bool     haveTap     = false;   // a first tap is waiting for its partner
static uint32_t tapReleased = 0;

static void registerTap(uint32_t pressedAt) {
    if (haveTap && pressedAt - tapReleased <= DOUBLE_TAP_MS) {
        haveTap = false;
        sendPacket('N', nullptr, 0);
        flash(200, 0, 255);            // violet: double tap (clear)
    } else {
        haveTap     = true;            // wait to see if a second tap follows
        tapReleased = millis();
    }
}

// A lone tap is only known to be single once the double-tap window has passed
// without another press.
static void serviceTaps() {
    if (haveTap && !button.isPressed() && millis() - tapReleased > DOUBLE_TAP_MS) {
        haveTap = false;
        sendPacket('T', nullptr, 0);
        flash(0, 120, 255);            // blue: single tap (Enter / Tab)
    }
}

// ---- the program --------------------------------------------------------------
void mainTask(void* /*parameter*/) {
    esp_task_wdt_add(NULL);

    while (true) {
        esp_task_wdt_reset();
        button.update();
        readHost();

        bool pressed  = button.wasPressed();
        bool released = button.wasReleased();

        if (pressed) {
            pressedAt = millis();
            if (recording && tailing) {
                tailing    = false;            // re-pressed during the tail (or a bounce): keep going
                freshPress = false;
                setLight(L_RECORDING);
            } else if (!recording) {
                startRecording();              // start at once, so dictation has no lag
                freshPress = true;
            }
        }

        if (released && recording && !tailing) {
            if (millis() - pressedAt < TAP_MS) {
                // A tap, not speech. If it opened the recording, the recording
                // is nothing but the tap; if it came in a dictation's tail,
                // keep the dictation and end it here.
                if (freshPress) abortRecording();
                else            finishRecording();
                registerTap(pressedAt);
            } else {
                tailing    = true;
                releasedAt = millis();
                haveTap    = false;            // tap-then-hold is dictation, not a tap
            }
        }

        serviceTaps();

        if (recording) {
            drainAudio();
            bool tailDone = tailing && (millis() - releasedAt >= TAIL_MS);
            bool tooLong  = (millis() - recStart) >= MAX_RECORD_MS;
            if (tailDone || tooLong) finishRecording();
        }

        serviceLight();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void setup() {
    Serial.setTxBufferSize(8192);   // must precede begin()
    Serial.begin(115200);           // baud is ignored on native USB
    Serial.setTxTimeoutMs(0);       // never block the firmware if the host isn't reading

    audioStream = xStreamBufferCreate(STREAM_BYTES, 2);

    button.begin();
    button.setBrightnessCap(120);   // bright enough to see while walking around
    button.setBrightness(120);
    button.off();

    mic.onAudio(onMicAudio, nullptr);
    mic.begin();   // LR low = left slot, which is the slot the driver reads (verified)

    // Boot blink: green = alive.
    button.setColor(0, 255, 0);
    vTaskDelay(pdMS_TO_TICKS(250));
    button.off();

    esp_task_wdt_init(30, true);
    xTaskCreatePinnedToCore(mainTask, "MainTask", 16384, NULL, 1, &mainTaskHandle, 1);
}

void loop() {
    vTaskDelay(portMAX_DELAY);
}
