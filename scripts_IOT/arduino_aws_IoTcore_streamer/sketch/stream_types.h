#pragma once
#include <stdint.h>

// Types live in a header so the Arduino builder's auto-generated
// prototypes (inserted above the sketch body) can see them.

const uint8_t  BLOCK   = 24;    // samples per Bridge message (one sensor)
const uint16_t BUF_CAP = 96;    // per-sensor sample buffer (4 blocks)

struct Adxl {
  uint8_t  addr;
  bool     present;
  int16_t  buf[BUF_CAP][3];     // pending samples, x/y/z raw LSB
  uint16_t count;               // samples waiting in buf
  uint32_t seq;                 // messages sent for this sensor
  uint32_t fifoFull;            // times the 32-deep FIFO was found full
  uint32_t dropped;             // samples lost because buf was full

  // 0.5 s statistics window for the OLED
  float    sum[3];
  float    sumsq[3];
  uint16_t n;
  float    rms[3];              // g, mean removed
  float    rmsTotal;            // g, root-sum-square of the three axes
};
