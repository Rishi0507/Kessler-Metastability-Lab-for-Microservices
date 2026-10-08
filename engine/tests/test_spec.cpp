#include <string>

#include "../src/sim.hpp"
#include "../src/spec.hpp"
#include "harness.hpp"

using namespace kessler;

namespace {

std::string error_of(const std::string& text) {
  try {
    parse_spec_text(text);
  } catch (const SpecError& e) {
    return e.what();
  }
  return "";
}

bool contains(const std::string& hay, const std::string& needle) { return hay.find(needle) != std::string::npos; }

}

TEST_CASE(spec_defaults_and_unit_conversion) {
  const Spec s = parse_spec_text(R"({"traffic":{"rps":50,"client":{"timeout_ms":1500}},
    "services":[{"name":"a","service_time":{"dist":"constant","ms":4},
      "calls":[{"to":"b","timeout_ms":250,"retry":{"max_attempts":3,"base_ms":20,"jitter":"equal"}}]},
     {"name":"b","service_time":{"dist":"exponential","mean_ms":2}}]})");
  CHECK(s.services.size() == 2);
  CHECK(s.entry == 0);
  CHECK_NEAR(s.client.timeout_s, 1.5, 1e-12);
  CHECK_NEAR(s.services[0].calls[0].timeout_s, 0.25, 1e-12);
  CHECK(s.services[0].calls[0].retry.max_attempts == 3);
  CHECK(s.services[0].calls[0].retry.jitter == Jitter::Equal);
  CHECK_NEAR(s.services[0].service_time.mean(), 0.004, 1e-12);
  CHECK(!s.services[0].calls[0].budget.enabled);
}

TEST_CASE(spec_errors_are_path_precise) {
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[]})"), "spec.services"));
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[{"name":"a","service_time":{"dist":"constant","ms":1},
      "calls":[{"to":"ghost"}]}]})"), "spec.services[0].calls[0].to: unknown service 'ghost'"));
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[{"name":"a","instances":0,
      "service_time":{"dist":"constant","ms":1}}]})"), "spec.services[0].instances"));
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[{"name":"a",
      "service_time":{"dist":"lognormal","p50_ms":10,"p99_ms":5}}]})"), "p99_ms"));
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[{"name":"a","service_time":{"dist":"constant","ms":1}},
      {"name":"a","service_time":{"dist":"constant","ms":1}}]})"), "duplicate service"));
  CHECK(contains(error_of(R"({"traffic":{"rps":1},"services":[{"name":"a","service_time":{"dist":"constant","ms":1}}],
      "faults":[{"kind":"meteor","target":"a"}]})"), "unknown fault"));
  CHECK(contains(error_of("{not json"), "invalid JSON"));
}

TEST_CASE(spec_rejects_call_cycles) {
  const std::string e = error_of(R"({"traffic":{"rps":1},"services":[
    {"name":"a","service_time":{"dist":"constant","ms":1},"calls":[{"to":"b"}]},
    {"name":"b","service_time":{"dist":"constant","ms":1},"calls":[{"to":"c"}]},
    {"name":"c","service_time":{"dist":"constant","ms":1},"calls":[{"to":"a"}]}]})");
  CHECK(contains(e, "cycle"));
}

TEST_CASE(static_analysis_capacity_and_amplification) {
  const Spec s = parse_spec_text(R"({"traffic":{"rps":600,"entry":"api",
      "client":{"retry":{"max_attempts":3}}},
    "network":{"latency_ms":0,"jitter_ms":0},
    "services":[
      {"name":"api","instances":4,"concurrency":32,"service_time":{"dist":"constant","ms":2},
       "calls":[{"to":"db","retry":{"max_attempts":3}}]},
      {"name":"db","instances":1,"concurrency":8,"service_time":{"dist":"constant","ms":8}}]})");
  const json::Value a = json::parse(static_analysis_json(s));
  CHECK(a.find("bottleneck")->as_string() == "db");
  CHECK_NEAR(a.find("max_rps")->as_number(), 1000.0, 1e-6);
  CHECK_NEAR(a.find("worst_amplification")->as_number(), 9.0, 1e-9);
  const auto& svcs = a.find("services")->items();
  CHECK_NEAR(svcs[0].find("capacity_rps")->as_number(), 12800.0, 1e-6);
  CHECK_NEAR(svcs[1].find("utilization")->as_number(), 0.6, 1e-9);
  CHECK(a.find("worst_amplification_path")->items().size() == 3);
}
