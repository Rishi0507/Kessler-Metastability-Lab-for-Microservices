#pragma once

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace kt {

struct Case {
  const char* name;
  void (*fn)();
};

inline std::vector<Case>& registry() {
  static std::vector<Case> r;
  return r;
}

struct Registrar {
  Registrar(const char* n, void (*f)()) { registry().push_back({n, f}); }
};

struct Abort {};

inline int& failures() {
  static int f = 0;
  return f;
}

inline void fail(const char* file, int line, const std::string& msg) {
  std::printf("    FAIL %s:%d: %s\n", file, line, msg.c_str());
  ++failures();
}

}

#define KT_CAT2(a, b) a##b
#define KT_CAT(a, b) KT_CAT2(a, b)

#define TEST_CASE(name)                                               \
  static void name();                                                 \
  static ::kt::Registrar KT_CAT(kt_reg_, name)(#name, &name);         \
  static void name()

#define CHECK(cond)                                                   \
  do {                                                                \
    if (!(cond)) ::kt::fail(__FILE__, __LINE__, "CHECK(" #cond ")");  \
  } while (0)

#define REQUIRE(cond)                                                    \
  do {                                                                   \
    if (!(cond)) {                                                       \
      ::kt::fail(__FILE__, __LINE__, "REQUIRE(" #cond ")");              \
      throw ::kt::Abort{};                                               \
    }                                                                    \
  } while (0)

#define CHECK_NEAR(a, b, tol)                                                                     \
  do {                                                                                            \
    const double kt_a = (a), kt_b = (b), kt_t = (tol);                                            \
    if (!(std::fabs(kt_a - kt_b) <= kt_t)) {                                                      \
      char kt_buf[256];                                                                           \
      std::snprintf(kt_buf, sizeof kt_buf, "CHECK_NEAR(%s, %s): %.6g vs %.6g (tol %.4g)", #a, #b, \
                    kt_a, kt_b, kt_t);                                                            \
      ::kt::fail(__FILE__, __LINE__, kt_buf);                                                     \
    }                                                                                             \
  } while (0)

#define CHECK_THROWS(expr)                                                     \
  do {                                                                         \
    bool kt_threw = false;                                                     \
    try {                                                                      \
      expr;                                                                    \
    } catch (...) {                                                            \
      kt_threw = true;                                                         \
    }                                                                          \
    if (!kt_threw) ::kt::fail(__FILE__, __LINE__, "expected exception: " #expr); \
  } while (0)
