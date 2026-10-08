#include <algorithm>
#include <cmath>
#include <vector>

#include "../src/histogram.hpp"
#include "../src/rng.hpp"
#include "../src/spec.hpp"
#include "harness.hpp"

using namespace kessler;

TEST_CASE(rng_is_deterministic_and_streams_are_independent) {
  Rng a(123), b(123), c(124);
  bool all_equal = true, any_diff = false;
  for (int i = 0; i < 1000; ++i) {
    const uint64_t x = a.next(), y = b.next(), z = c.next();
    all_equal &= x == y;
    any_diff |= x != z;
  }
  CHECK(all_equal);
  CHECK(any_diff);
  Rng s1 = Rng::stream(42, 1), s2 = Rng::stream(42, 2);
  CHECK(s1.next() != s2.next());
}

TEST_CASE(rng_uniform_and_exponential_moments) {
  Rng r(7);
  const int n = 400000;
  double su = 0, se = 0;
  for (int i = 0; i < n; ++i) {
    const double u = r.uniform();
    REQUIRE(u >= 0.0 && u < 1.0);
    su += u;
    se += r.exponential(10.0);
  }
  CHECK_NEAR(su / n, 0.5, 0.003);
  CHECK_NEAR(se / n, 10.0, 0.08);
}

TEST_CASE(lognormal_fitted_from_p50_p99_reproduces_quantiles) {
  const Spec spec = parse_spec_text(R"({"traffic":{"rps":1},"services":[{"name":"s",
      "service_time":{"dist":"lognormal","p50_ms":5,"p99_ms":40}}]})");
  const Dist& d = spec.services[0].service_time;
  Rng r(99);
  std::vector<double> xs(300000);
  for (double& x : xs) x = d.sample(r) * 1000.0;
  std::sort(xs.begin(), xs.end());
  CHECK_NEAR(xs[xs.size() / 2], 5.0, 0.1);
  CHECK_NEAR(xs[static_cast<size_t>(xs.size() * 0.99)], 40.0, 1.5);
  CHECK_NEAR(d.p99() * 1000.0, 40.0, 1e-6);
}

TEST_CASE(histogram_percentiles_within_resolution) {
  Histogram h;
  for (int i = 1; i <= 10000; ++i) h.record(i * 0.1);
  CHECK(h.count() == 10000);
  CHECK_NEAR(h.mean(), 500.05, 1e-6);
  CHECK_NEAR(h.percentile(0.5), 500.0, 500.0 * 0.035);
  CHECK_NEAR(h.percentile(0.99), 990.0, 990.0 * 0.035);
  CHECK_NEAR(h.percentile(1.0), 1000.0, 1e-9);
  Histogram g;
  g.merge(h);
  g.merge(h);
  CHECK(g.count() == 20000);
  CHECK_NEAR(g.percentile(0.5), h.percentile(0.5), 1e-9);
}
