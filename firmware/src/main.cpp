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
 *     'M'  three or more taps           (len 1: uint8 tap count)
 *          host: 3 = open the window picker; while it's open, the next
 *          taps (T = 1, N = 2, M = count) pick that window
 *     'C'  audio, IMA-ADPCM compressed (BLE only; USB sends 'D'):
 *          int16 predictor, uint8 step index, uint16 sample count, then
 *          4-bit codes, low nibble first. Each block decodes on its own.
 *   Driver log lines ("[Button] Pressed" etc.) may appear *between* packets;
 *   the host skips anything that isn't a packet.
 *
 * Bluetooth LE carries exactly the same packets, as a byte stream: the host
 * subscribes to the TX characteristic (notifications) and writes the
 * host -> device bytes below to the RX characteristic. While a BLE host is
 * subscribed, all packets go over BLE instead of USB. Audio is ADPCM there
 * (64 kbit/s instead of 256), which BLE carries comfortably.
 *
 * Host -> device (single bytes, drive the button light):
 *   'B' busy/transcribing (amber)   'K' done (green flash)
 *   'X' error (red flash)           'I' idle (off)
 *   '?' identify: the device answers with an 'H' packet. The host uses this
 *       to tell eve apart from other ESP32 boards on the same computer.
 */

#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "esp_task_wdt.h"

#include <NimBLEDevice.h>

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

// ---- Bluetooth LE ------------------------------------------------------------
// UUIDs: eve's own service (any random 128-bit base would do).
#define EVE_SVC_UUID "7e5e0001-6576-6500-8000-00805f9b34fb"
#define EVE_TX_UUID  "7e5e0002-6576-6500-8000-00805f9b34fb"   // device -> host, notify
#define EVE_RX_UUID  "7e5e0003-6576-6500-8000-00805f9b34fb"   // host -> device, write

static NimBLECharacteristic* bleTx = nullptr;
static volatile bool     bleSubscribed = false;
static volatile uint16_t bleConn = 0xFFFF;
static QueueHandle_t     bleRx = nullptr;          // host -> device bytes

class EveServerCB : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* s, ble_gap_conn_desc* d) override {
        bleConn = d->conn_handle;
        // Ask for a short connection interval (7.5-15 ms): low latency audio.
        s->updateConnParams(d->conn_handle, 6, 12, 0, 400);
    }
    void onDisconnect(NimBLEServer* /*s*/) override {
        bleSubscribed = false;
        bleConn = 0xFFFF;
        NimBLEDevice::startAdvertising();
    }
};

class EveTxCB : public NimBLECharacteristicCallbacks {
    void onSubscribe(NimBLECharacteristic* /*c*/, ble_gap_conn_desc* /*d*/, uint16_t sub) override {
        bleSubscribed = (sub & 1) != 0;
    }
};

class EveRxCB : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        std::string v = c->getValue();
        for (char ch : v) xQueueSend(bleRx, &ch, 0);
    }
};

static void bleBegin() {
    bleRx = xQueueCreate(64, 1);
    NimBLEDevice::init("eve");
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    NimBLEDevice::setMTU(247);
    NimBLEServer* server = NimBLEDevice::createServer();
    server->setCallbacks(new EveServerCB());
    NimBLEService* svc = server->createService(EVE_SVC_UUID);
    bleTx = svc->createCharacteristic(EVE_TX_UUID, NIMBLE_PROPERTY::NOTIFY);
    bleTx->setCallbacks(new EveTxCB());
    NimBLECharacteristic* rx = svc->createCharacteristic(
        EVE_RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
    rx->setCallbacks(new EveRxCB());
    svc->start();
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(EVE_SVC_UUID);
    adv->setScanResponse(true);   // the name goes in the scan response
    NimBLEDevice::startAdvertising();
}

// One notification, retrying briefly while the stack is out of buffers.
static bool bleNotify(const uint8_t* p, size_t n) {
    uint32_t t0 = millis();
    while (true) {
        uint16_t conn = bleConn;
        if (!bleSubscribed || conn == 0xFFFF) return false;
        struct os_mbuf* om = ble_hs_mbuf_from_flat(p, n);
        if (om) {
            int rc = ble_gattc_notify_custom(conn, bleTx->getHandle(), om);   // consumes om
            if (rc == 0) return true;
            if (rc != BLE_HS_ENOMEM && rc != BLE_HS_EBUSY) return false;
        }
        if (millis() - t0 > 100) return false;
        vTaskDelay(1);
    }
}

static bool bleWrite(const uint8_t* p, size_t n) {
    NimBLEServer* s = NimBLEDevice::getServer();
    uint16_t conn = bleConn;
    size_t chunk = (s && conn != 0xFFFF) ? s->getPeerMTU(conn) : 23;
    chunk = chunk > 3 ? chunk - 3 : 20;
    for (size_t off = 0; off < n; off += chunk) {
        if (!bleNotify(p + off, min(chunk, n - off))) return false;
    }
    return true;
}

static inline bool useBle() { return bleSubscribed; }

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
    static uint8_t pkt[5 + PACKET_BYTES];
    if (len > PACKET_BYTES) return false;
    pkt[0] = 0xA5; pkt[1] = 0x5A; pkt[2] = (uint8_t)type;
    pkt[3] = (uint8_t)(len & 0xFF); pkt[4] = (uint8_t)(len >> 8);
    if (len) memcpy(pkt + 5, payload, len);
    return useBle() ? bleWrite(pkt, 5 + len) : writeAll(pkt, 5 + len);
}

// ---- IMA ADPCM (for BLE) ----------------------------------------------------
static const int16_t IMA_STEP[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767};
static const int8_t IMA_INDEX[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};
static int32_t adpcmPred = 0;
static int     adpcmIndex = 0;

// Encodes n samples into out (header + ceil(n/2) bytes). Returns its length.
static size_t adpcmEncode(const int16_t* in, size_t n, uint8_t* out) {
    out[0] = (uint8_t)(adpcmPred & 0xFF); out[1] = (uint8_t)((adpcmPred >> 8) & 0xFF);
    out[2] = (uint8_t)adpcmIndex;
    out[3] = (uint8_t)(n & 0xFF); out[4] = (uint8_t)(n >> 8);
    uint8_t* d = out + 5;
    for (size_t i = 0; i < n; i++) {
        int step = IMA_STEP[adpcmIndex];
        int diff = in[i] - adpcmPred;
        int code = 0;
        if (diff < 0) { code = 8; diff = -diff; }
        int delta = step >> 3;
        if (diff >= step)        { code |= 4; diff -= step; delta += step; }
        if (diff >= (step >> 1)) { code |= 2; diff -= step >> 1; delta += step >> 1; }
        if (diff >= (step >> 2)) { code |= 1; delta += step >> 2; }
        adpcmPred += (code & 8) ? -delta : delta;
        if (adpcmPred > 32767) adpcmPred = 32767;
        if (adpcmPred < -32768) adpcmPred = -32768;
        adpcmIndex += IMA_INDEX[code];
        if (adpcmIndex < 0) adpcmIndex = 0;
        if (adpcmIndex > 88) adpcmIndex = 88;
        if (i & 1) d[i >> 1] |= (uint8_t)(code << 4);
        else       d[i >> 1] = (uint8_t)code;
    }
    return 5 + (n + 1) / 2;
}

static const size_t ADPCM_SAMPLES = 400;   // 25 ms per 'C' packet (205 bytes)

static void drainAudio() {
    static uint8_t chunk[PACKET_BYTES] __attribute__((aligned(4)));   // read as int16
    static uint8_t coded[5 + ADPCM_SAMPLES / 2 + 1];
    // Bound the work per loop so the watchdog and button stay serviced.
    for (int i = 0; i < 8; i++) {
        size_t avail = xStreamBufferBytesAvailable(audioStream);
        if (avail < 2) return;
        if (useBle()) {
            // Whole blocks while recording; the remainder once capture stopped.
            if (avail < ADPCM_SAMPLES * 2 && capturing) return;
            size_t want = min(avail, ADPCM_SAMPLES * 2) & ~(size_t)1;
            size_t got = xStreamBufferReceive(audioStream, chunk, want, 0) & ~(size_t)1;
            if (got) sendPacket('C', coded, (uint16_t)adpcmEncode((const int16_t*)chunk, got / 2, coded));
        } else {
            size_t want = min(avail, PACKET_BYTES) & ~(size_t)1;   // keep it even
            size_t got = xStreamBufferReceive(audioStream, chunk, want, 0);
            if (got) sendPacket('D', chunk, (uint16_t)got);
        }
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

static int nextHostByte() {
    if (Serial.available() > 0) return Serial.read();
    char c;
    if (bleRx && xQueueReceive(bleRx, &c, 0) == pdTRUE) return (uint8_t)c;
    return -1;
}

static void readHost() {
    int c;
    while ((c = nextHostByte()) >= 0) {
        if (c == '?') {                             // "who are you?" from the host
            sendPacket('H', (const uint8_t*)"eve", 3);
            continue;
        }
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
    adpcmPred = 0;
    adpcmIndex = 0;
    sendPacket('S', nullptr, 0);
    recording = true;
    tailing   = false;
    recStart  = millis();
    capturing = true;
    setLight(L_RECORDING);
}

static void finishRecording() {
    capturing = false;
    for (int i = 0; i < 4; i++) drainAudio();
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
// Taps less than DOUBLE_TAP_MS apart count as one gesture. Its count is only
// known once that much time has passed with no further press.
static uint8_t  tapCount    = 0;       // taps in the gesture so far (0 = none waiting)
static uint32_t tapReleased = 0;

static void registerTap(uint32_t pressedAt) {
    if (tapCount && pressedAt - tapReleased <= DOUBLE_TAP_MS) {
        if (tapCount < 255) tapCount++;
    } else {
        tapCount = 1;
    }
    tapReleased = millis();
}

static void serviceTaps() {
    if (tapCount && !button.isPressed() && millis() - tapReleased > DOUBLE_TAP_MS) {
        uint8_t n = tapCount;
        tapCount = 0;
        if (n == 1) {
            sendPacket('T', nullptr, 0);
            flash(0, 120, 255);        // blue: single tap (Enter / Tab)
        } else if (n == 2) {
            sendPacket('N', nullptr, 0);
            flash(200, 0, 255);        // violet: double tap (clear)
        } else {
            sendPacket('M', &n, 1);
            flash(255, 255, 255);      // white: three or more taps (window picker)
        }
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
                tapCount   = 0;                // tap-then-hold is dictation, not a tap
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

    bleBegin();

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
