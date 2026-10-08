#include <chrono>
#include <cstdio>
#include <cstring>
#include <exception>

#include "harness.hpp"

int main(int argc, char** argv) {
  const char* filter = argc > 1 ? argv[1] : nullptr;
  int run = 0, failed_cases = 0;
  const auto t0 = std::chrono::steady_clock::now();
  for (const kt::Case& c : kt::registry()) {
    if (filter && !std::strstr(c.name, filter)) continue;
    ++run;
    const int before = kt::failures();
    const auto a = std::chrono::steady_clock::now();
    try {
      c.fn();
    } catch (const kt::Abort&) {
    } catch (const std::exception& e) {
      kt::fail(__FILE__, __LINE__, std::string("unexpected exception: ") + e.what());
    }
    const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - a).count();
    const bool ok = kt::failures() == before;
    if (!ok) ++failed_cases;
    std::printf("[%s] %-48s %8.1f ms\n", ok ? " OK " : "FAIL", c.name, ms);
  }
  const double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::printf("\n%d test cases, %d passed, %d failed (%.2fs)\n", run, run - failed_cases, failed_cases, s);
  return failed_cases == 0 ? 0 : 1;
}
