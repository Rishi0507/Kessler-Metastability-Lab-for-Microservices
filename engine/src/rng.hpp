#pragma once

#include <cmath>
#include <cstdint>

namespace kessler {

inline uint64_t splitmix64(uint64_t& state) {
  uint64_t z = (state += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

class Rng {
 public:
  explicit Rng(uint64_t seed = 1) { reseed(seed); }

  static Rng stream(uint64_t master_seed, uint64_t stream_id) {
    uint64_t x = master_seed ^ (0xD1B54A32D192ED03ull * (stream_id + 1));
    return Rng(splitmix64(x));
  }

  void reseed(uint64_t seed) {
    uint64_t x = seed;
    for (auto& v : s_) v = splitmix64(x);
    has_spare_ = false;
  }

  uint64_t next() {
    const uint64_t result = rotl(s_[1] * 5, 7) * 9;
    const uint64_t t = s_[1] << 17;
    s_[2] ^= s_[0];
    s_[3] ^= s_[1];
    s_[1] ^= s_[2];
    s_[0] ^= s_[3];
    s_[2] ^= t;
    s_[3] = rotl(s_[3], 45);
    return result;
  }

  double uniform() { return static_cast<double>(next() >> 11) * 0x1.0p-53; }
  double uniform_open() { return (static_cast<double>(next() >> 11) + 0.5) * 0x1.0p-53; }
  double uniform(double lo, double hi) { return lo + (hi - lo) * uniform(); }

  uint32_t below(uint32_t n) {
    return static_cast<uint32_t>(((next() >> 32) * static_cast<uint64_t>(n)) >> 32);
  }

  double exponential(double mean) { return -mean * std::log(uniform_open()); }

  double normal() {
    if (has_spare_) { has_spare_ = false; return spare_; }
    double u, v, s;
    do {
      u = 2.0 * uniform() - 1.0;
      v = 2.0 * uniform() - 1.0;
      s = u * u + v * v;
    } while (s >= 1.0 || s == 0.0);
    const double m = std::sqrt(-2.0 * std::log(s) / s);
    spare_ = v * m;
    has_spare_ = true;
    return u * m;
  }

  double lognormal(double mu, double sigma) { return std::exp(mu + sigma * normal()); }

 private:
  static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
  uint64_t s_[4]{};
  double spare_ = 0.0;
  bool has_spare_ = false;
};

inline constexpr double kZ99 = 2.3263478740408408;

}
