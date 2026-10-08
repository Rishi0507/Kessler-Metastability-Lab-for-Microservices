#include <cmath>
#include <string>

#include "../src/sim.hpp"
#include "../src/spec.hpp"
#include "harness.hpp"

using namespace kessler;

namespace {

Spec queue_spec(double lambda, int servers, double mean_ms, double horizon_s, uint64_t seed) {
  std::string text = R"({"seed":)" + std::to_string(seed) + R"(,"horizon_s":)" + std::to_string(horizon_s) +
                     R"(,"warmup_s":100,"network":{"latency_ms":0,"jitter_ms":0},
      "traffic":{"rps":)" + std::to_string(lambda) +
                     R"(,"client":{"timeout_ms":3600000,"slo_ms":3600000}},
      "services":[{"name":"q","instances":1,"concurrency":)" +
                     std::to_string(servers) + R"(,"queue":{"capacity":10000000},
        "service_time":{"dist":"exponential","mean_ms":)" +
                     std::to_string(mean_ms) + R"(}}]})";
  return parse_spec_text(text);
}

double erlang_c(int c, double a) {
  double sum = 0, term = 1;
  for (int k = 0; k < c; ++k) {
    if (k > 0) term *= a / k;
    sum += term;
  }
  double top = term * a / c;
  top *= c / (c - a);
  return top / (sum + top);
}

}

TEST_CASE(mm1_mean_sojourn_matches_theory) {
  Simulator sim(queue_spec(70, 1, 10, 3100, 5));
  sim.run();
  const ServiceTotals& t = sim.service_totals(0);
  REQUIRE(t.ok > 150000);
  CHECK_NEAR(t.latency.mean(), 1000.0 / 30.0, (1000.0 / 30.0) * 0.05);
  CHECK(t.err == 0);
}

TEST_CASE(mmc_mean_sojourn_matches_erlang_c) {
  const int c = 4;
  const double lambda = 300, mu = 100;
  const double pw = erlang_c(c, lambda / mu);
  const double wq = pw / (c * mu - lambda);
  const double w_ms = (wq + 1.0 / mu) * 1000.0;
  Simulator sim(queue_spec(lambda, c, 10, 2100, 9));
  sim.run();
  const ServiceTotals& t = sim.service_totals(0);
  REQUIRE(t.ok > 500000);
  CHECK_NEAR(pw, 0.5094, 0.0005);
  CHECK_NEAR(t.latency.mean(), w_ms, w_ms * 0.04);
}

TEST_CASE(little_law_holds_for_client_view) {
  Simulator sim(queue_spec(200, 4, 10, 600, 3));
  sim.run();
  const ClientTotals& c = sim.client_totals();
  const double secs = 600 - 100;
  CHECK_NEAR(c.ok / secs, 200.0, 200.0 * 0.02);
  CHECK(c.err == 0);
}
