#pragma once

#include <algorithm>
#include <atomic>
#include <cstddef>
#include <thread>
#include <vector>

namespace kessler {

inline int default_threads() {
  const unsigned hc = std::thread::hardware_concurrency();
  return hc == 0 ? 1 : static_cast<int>(hc);
}

template <class Fn>
void parallel_for(size_t n, int threads, Fn&& fn) {
  threads = std::max(1, std::min<int>(threads, static_cast<int>(n)));
  std::atomic<size_t> next{0};
  auto worker = [&]() {
    for (;;) {
      const size_t i = next.fetch_add(1, std::memory_order_relaxed);
      if (i >= n) return;
      fn(i);
    }
  };
  if (threads == 1) {
    worker();
    return;
  }
  std::vector<std::jthread> pool;
  pool.reserve(static_cast<size_t>(threads));
  for (int t = 0; t < threads; ++t) pool.emplace_back(worker);
}

}
