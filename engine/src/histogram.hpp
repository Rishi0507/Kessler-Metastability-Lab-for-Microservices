#pragma once

#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>

namespace kessler {

class Histogram {
 public:
  static constexpr int kSub = 16;
  static constexpr int kBuckets = 1 + 32 * kSub;

  void record(double ms) {
    ++counts_[static_cast<size_t>(index(ms))];
    ++n_;
    sum_ += ms;
    if (ms > max_) max_ = ms;
  }

  void merge(const Histogram& o) {
    for (int i = 0; i < kBuckets; ++i) counts_[static_cast<size_t>(i)] += o.counts_[static_cast<size_t>(i)];
    n_ += o.n_;
    sum_ += o.sum_;
    if (o.max_ > max_) max_ = o.max_;
  }

  void reset() {
    counts_.fill(0);
    n_ = 0;
    sum_ = 0.0;
    max_ = 0.0;
  }

  uint64_t count() const { return n_; }
  double mean() const { return n_ ? sum_ / static_cast<double>(n_) : 0.0; }
  double max() const { return max_; }

  double percentile(double q) const {
    if (n_ == 0) return 0.0;
    if (q <= 0) q = 0;
    if (q >= 1) return max_;
    const double rank = q * static_cast<double>(n_);
    uint64_t cum = 0;
    for (int i = 0; i < kBuckets; ++i) {
      cum += counts_[static_cast<size_t>(i)];
      if (static_cast<double>(cum) >= rank && counts_[static_cast<size_t>(i)] > 0) {
        const double v = value_of(i);
        return v < max_ ? v : max_;
      }
    }
    return max_;
  }

 private:
  static int index(double ms) {
    const double us = ms * 1000.0;
    if (!(us >= 1.0)) return 0;
    int e = 0;
    const double m = std::frexp(us, &e);
    int sub = static_cast<int>((m - 0.5) * 2.0 * kSub);
    if (sub >= kSub) sub = kSub - 1;
    const int idx = 1 + (e - 1) * kSub + sub;
    return idx < kBuckets ? idx : kBuckets - 1;
  }

  static double value_of(int idx) {
    if (idx == 0) return 0.0005;
    const int e = (idx - 1) / kSub + 1;
    const int sub = (idx - 1) % kSub;
    const double lo = std::ldexp(0.5 + static_cast<double>(sub) / (2.0 * kSub), e);
    const double hi = std::ldexp(0.5 + static_cast<double>(sub + 1) / (2.0 * kSub), e);
    return 0.5 * (lo + hi) / 1000.0;
  }

  std::array<uint32_t, kBuckets> counts_{};
  uint64_t n_ = 0;
  double sum_ = 0.0;
  double max_ = 0.0;
};

}
