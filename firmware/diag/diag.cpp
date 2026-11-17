// Microphone wiring diagnostic for the Atech Tiny. Not part of the product
// firmware — build with `pio run -e diag -t upload`.
//
// For every plausible pin/channel combination it captures 1024 raw stereo
// frames and reports, per I2S slot, how many distinct 24-bit values it saw.
// A live ICS-43434 gives hundreds of distinct values even in a quiet room;
// a dead or tri-stated line gives ~1.

#include <Arduino.h>
#include <driver/i2s.h>

struct Combo { const char* name; int lr, ws, sck, sd; };
static const Combo combos[] = {
    {"as documented     ", 41, 40, 17, 18},
    {"sck<->sd swapped  ", 41, 40, 18, 17},
    {"lr<->ws swapped   ", 40, 41, 17, 18},
    {"both swapped      ", 40, 41, 18, 17},
    // module inserted the other way round: its port-1 half in board port 2
    {"reversed module   ", 17, 18, 41, 40},
    {"reversed, sck/sd  ", 17, 18, 40, 41},
    {"reversed, lr/ws   ", 18, 17, 41, 40},
    {"reversed, both    ", 18, 17, 40, 41},
};

static int32_t raw[1024 * 2];

static void analyse(int slot, int frames, int& uniq, int32_t& lo, int32_t& hi, uint32_t& first) {
    static int32_t vals[1024];
    for (int i = 0; i < frames; i++) vals[i] = raw[i * 2 + slot] >> 8;   // 24-bit
    first = (uint32_t)raw[slot];
    lo = hi = vals[0];
    uniq = 0;
    // O(n^2) on 1024 is fine for a diagnostic
    for (int i = 0; i < frames; i++) {
        if (vals[i] < lo) lo = vals[i];
        if (vals[i] > hi) hi = vals[i];
        bool seen = false;
        for (int j = 0; j < i && !seen; j++) seen = vals[j] == vals[i];
        if (!seen) uniq++;
    }
}

static void runCombo(const Combo& c, int lrLevel) {
    pinMode(c.lr, OUTPUT);
    digitalWrite(c.lr, lrLevel);

    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
    cfg.sample_rate = 16000;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.dma_buf_count = 4;
    cfg.dma_buf_len = 256;
    i2s_pin_config_t pins = {};
    pins.bck_io_num = c.sck;
    pins.ws_io_num = c.ws;
    pins.data_out_num = I2S_PIN_NO_CHANGE;
    pins.data_in_num = c.sd;

    i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_1, &pins);
    delay(300);                                   // mic start-up + flush
    size_t got = 0;
    i2s_read(I2S_NUM_1, raw, sizeof(raw), &got, pdMS_TO_TICKS(500));   // discard
    i2s_read(I2S_NUM_1, raw, sizeof(raw), &got, pdMS_TO_TICKS(500));
    i2s_driver_uninstall(I2S_NUM_1);
    pinMode(c.lr, INPUT);
    pinMode(c.ws, INPUT);

    int frames = got / 8;
    for (int slot = 0; slot < 2; slot++) {
        int uniq; int32_t lo, hi; uint32_t first;
        analyse(slot, frames, uniq, lo, hi, first);
        Serial.printf("DIAG %s LR=%d slot=%d frames=%d unique=%4d min=%8ld max=%8ld raw0=%08lx\n",
                      c.name, lrLevel, slot, frames, uniq, (long)lo, (long)hi, (unsigned long)first);
    }
}

void setup() {
    Serial.begin(115200);
    delay(1500);
}

void loop() {
    Serial.println("DIAG ---- begin ----");
    for (const Combo& c : combos)
        for (int lr = 0; lr <= 1; lr++) runCombo(c, lr);
    Serial.println("DIAG ---- end ----");
    delay(3000);
}
