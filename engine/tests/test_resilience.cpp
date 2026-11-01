#include <string>

#include "../src/json.hpp"
#include "../src/sim.hpp"
#include "../src/spec.hpp"
#include "harness.hpp"

using namespace kessler;

namespace {

std::string storm_spec(double load, bool fixed, uint64_t seed = 11) {
  const std::string jitter = fixed ? "full" : "none";
  const std::string budget = fixed ? R"(,"budget":{"ratio":0.1,"min_per_s":5})" : "";
  return R"({"seed":)" + std::to_string(seed) + R"(,"horizon_s":200,"warmup_s":30,
    "resilience":{"deadline_propagation":)" + std::string(fixed ? "true" : "false") + R"(},
    "traffic":{"rps":600,"load_multiplier":)" + std::to_string(load) + R"(,"entry":"api",
      "client":{"timeout_ms":1000,"retry":{"max_attempts":3,"base_ms":100,"cap_ms":400,"jitter":")" + jitter + "\"}" + budget + R"(}},
    "services":[
      {"name":"api","instances":4,"concurrency":32,"service_time":{"dist":"lognormal","p50_ms":2,"p99_ms":8},
       "calls":[{"to":"db","timeout_ms":250,"retry":{"max_attempts":3,"base_ms":20,"cap_ms":100,"jitter":")" + jitter + "\"}" + budget + R"(}]},
      {"name":"db","instances":1,"concurrency":8,"queue":{"capacity":2000},
       "service_time":{"dist":"lognormal","p50_ms":6,"p99_ms":20}}],
    "faults":[{"kind":"latency","target":"db","magnitude":5,"start_s":60,"duration_s":20}]})";
}

RunSummary run(const std::string& text) {
  Simulator sim(parse_spec_text(text));
  sim.run();
  return sim.summary();
}

const json::Value* service(const json::Value& frame, const char* name) {
  for (const json::Value& s : frame.find("services")->items()) {
    if (s.find("name")->as_string() == name) return &s;
  }
  return nullptr;
}

}

TEST_CASE(retry_storm_becomes_metastable) {
  const RunSummary r = run(storm_spec(1.0, false));
  CHECK(r.verdict == Verdict::Metastable);
  CHECK(r.peak_amp > 3.0);
  CHECK(r.wasted_fraction > 0.3);
  CHECK(r.min_goodput_ratio < 0.2);
}

TEST_CASE(budgets_deadlines_and_jitter_restore_recovery) {
  const RunSummary r = run(storm_spec(1.0, true));
  CHECK(r.verdict == Verdict::Recovered || r.verdict == Verdict::Stable);
  CHECK(r.recovery_s >= 0 && r.recovery_s < 30);
  CHECK(r.peak_amp < 2.0);
}

TEST_CASE(low_load_absorbs_the_same_trigger) {
  const RunSummary r = run(storm_spec(0.3, false));
  CHECK(r.verdict == Verdict::Stable || (r.verdict == Verdict::Recovered && r.recovery_s < 15));
}

TEST_CASE(deadline_propagation_alone_breaks_the_storm_slowly) {
  const std::string base = storm_spec(1.0, false);
  std::string with = base;
  with.replace(with.find("\"deadline_propagation\":false"), 28, "\"deadline_propagation\":true ");
  Simulator a(parse_spec_text(base)), b(parse_spec_text(with));
  a.run();
  b.run();
  const RunSummary ra = a.summary(), rb = b.summary();
  CHECK(ra.verdict == Verdict::Metastable);
  CHECK(rb.verdict == Verdict::Recovered);
  CHECK(rb.wasted_fraction < ra.wasted_fraction);
  CHECK(a.service_totals(1).dropped == 0);
  CHECK(b.service_totals(1).dropped > 10000);
}

TEST_CASE(cold_cache_collapse_is_metastable_only_at_high_load) {
  auto spec = [](double load) {
    return R"({"seed":3,"horizon_s":180,"warmup_s":30,
      "traffic":{"rps":1500,"load_multiplier":)" + std::to_string(load) + R"(,"entry":"api","client":{"timeout_ms":1000}},
      "services":[
        {"name":"api","instances":4,"concurrency":64,"service_time":{"dist":"lognormal","p50_ms":1,"p99_ms":4},
         "calls":[{"to":"cache","timeout_ms":400,"retry":{"max_attempts":2,"base_ms":10,"jitter":"none"}}]},
        {"name":"cache","instances":2,"concurrency":128,"service_time":{"dist":"lognormal","p50_ms":0.3,"p99_ms":1},
         "cache":{"hit_ratio":0.95,"key_space":50000,"miss_target":"db"},
         "calls":[{"to":"db","timeout_ms":200,"retry":{"max_attempts":3,"base_ms":10,"jitter":"none"}}]},
        {"name":"db","instances":1,"concurrency":8,"queue":{"capacity":1000},
         "service_time":{"dist":"lognormal","p50_ms":8,"p99_ms":25}}],
      "faults":[{"kind":"cache_flush","target":"cache","start_s":60}]})";
  };
  CHECK(run(spec(1.0)).verdict == Verdict::Metastable);
  const RunSummary low = run(spec(0.2));
  CHECK(low.verdict == Verdict::Stable || low.verdict == Verdict::Recovered);
}

TEST_CASE(circuit_breaker_trips_and_heals) {
  Simulator sim(parse_spec_text(R"({"seed":5,"horizon_s":120,"warmup_s":20,
    "traffic":{"rps":300,"entry":"api","client":{"timeout_ms":1000}},
    "services":[
      {"name":"api","instances":2,"concurrency":32,"service_time":{"dist":"constant","ms":1},
       "calls":[{"to":"db","timeout_ms":200,
                 "breaker":{"failure_rate":0.5,"window_s":5,"min_requests":20,"open_s":2,"half_open_probes":3}}]},
      {"name":"db","instances":1,"concurrency":16,"service_time":{"dist":"constant","ms":4}}],
    "faults":[{"kind":"errors","target":"db","magnitude":1.0,"start_s":40,"duration_s":10}]})"));
  sim.run();
  CHECK(sim.service_totals(0).cb_rejects > 500);
  const RunSummary r = sim.summary();
  CHECK(r.verdict == Verdict::Recovered);
  CHECK(r.recovery_s < 10);
}

TEST_CASE(full_queue_sheds_load) {
  Simulator sim(parse_spec_text(R"({"seed":2,"horizon_s":90,"warmup_s":10,
    "traffic":{"rps":300,"client":{"timeout_ms":2000}},
    "services":[{"name":"api","instances":1,"concurrency":4,"queue":{"capacity":10},
      "service_time":{"dist":"constant","ms":10}}],
    "faults":[{"kind":"spike","magnitude":3,"start_s":30,"duration_s":10}]})"));
  sim.run();
  CHECK(sim.service_totals(0).shed > 1000);
  const RunSummary r = sim.summary();
  CHECK(r.verdict == Verdict::Recovered);
}

TEST_CASE(crashed_instances_restart_and_service_recovers) {
  Simulator sim(parse_spec_text(R"({"seed":4,"horizon_s":100,"warmup_s":20,
    "traffic":{"rps":200,"entry":"api","client":{"timeout_ms":1000}},
    "services":[
      {"name":"api","instances":2,"concurrency":16,"service_time":{"dist":"constant","ms":1},
       "calls":[{"to":"db","timeout_ms":300}]},
      {"name":"db","instances":2,"concurrency":16,"service_time":{"dist":"constant","ms":5}}],
    "faults":[{"kind":"crash","target":"db","start_s":40,"duration_s":10}]})"));
  sim.run();
  const RunSummary r = sim.summary();
  CHECK(r.verdict == Verdict::Recovered);
  CHECK(r.recovery_s < 5);
  CHECK(r.min_goodput_ratio < 0.1);
}

TEST_CASE(power_of_two_choices_beats_random_tail_latency) {
  auto spec = [](const char* lb) {
    return std::string(R"({"seed":21,"horizon_s":400,"warmup_s":50,"network":{"latency_ms":0,"jitter_ms":0},
      "traffic":{"rps":850,"entry":"front","client":{"timeout_ms":60000,"slo_ms":60000}},
      "services":[
        {"name":"front","instances":1,"concurrency":100000,"service_time":{"dist":"constant","ms":0.001},
         "calls":[{"to":"worker","timeout_ms":60000}]},
        {"name":"worker","instances":10,"concurrency":1,"lb":")") + lb + R"(","queue":{"capacity":1000000},
         "service_time":{"dist":"exponential","mean_ms":10}}]})";
  };
  Simulator random(parse_spec_text(spec("random")));
  Simulator p2c(parse_spec_text(spec("p2c")));
  random.run();
  p2c.run();
  const double r99 = random.client_totals().latency.percentile(0.99);
  const double p99 = p2c.client_totals().latency.percentile(0.99);
  CHECK(p99 < 0.6 * r99);
}

TEST_CASE(autoscaler_adds_capacity_under_load) {
  Simulator sim(parse_spec_text(R"({"seed":8,"horizon_s":80,"warmup_s":5,
    "traffic":{"rps":1000,"client":{"timeout_ms":2000}},
    "services":[{"name":"api","instances":2,"concurrency":8,"service_time":{"dist":"exponential","mean_ms":10},
      "autoscale":{"min":2,"max":8,"target_utilization":0.5,"interval_s":5,"provision_s":10,"cooldown_s":60}}]})"),
                true);
  sim.run();
  const json::Value frames = json::parse(sim.take_frames());
  REQUIRE(frames.is_array() && !frames.items().empty());
  const json::Value* api = service(frames.items().back(), "api");
  REQUIRE(api);
  CHECK(api->find("instances")->as_number() >= 3);
}

TEST_CASE(runs_are_bit_identical_for_the_same_seed) {
  const Spec s = parse_spec_text(storm_spec(0.9, false, 77));
  Simulator a(s), b(s);
  a.run();
  b.run();
  CHECK(a.events() == b.events());
  REQUIRE(a.series().size() == b.series().size());
  bool same = true;
  for (size_t i = 0; i < a.series().size(); ++i) {
    same &= a.series()[i].good == b.series()[i].good && a.series()[i].p99 == b.series()[i].p99 &&
            a.series()[i].amp == b.series()[i].amp;
  }
  CHECK(same);
  Simulator c(parse_spec_text(storm_spec(0.9, false, 78)));
  c.run();
  CHECK(c.events() != a.events());
}

TEST_CASE(live_commands_inject_and_clear_faults) {
  Simulator sim(parse_spec_text(R"({"seed":1,"horizon_s":120,"warmup_s":5,
    "traffic":{"rps":100,"entry":"api"},
    "services":[{"name":"api","service_time":{"dist":"constant","ms":1},"calls":[{"to":"db"}]},
                {"name":"db","service_time":{"dist":"constant","ms":2}}]})"),
                true);
  sim.advance_to(10);
  CHECK(sim.command(R"({"op":"fault","fault":{"kind":"latency","target":"db","magnitude":4}})").empty());
  sim.advance_to(20);
  json::Value f = json::parse(sim.take_frames());
  CHECK_NEAR(service(f.items().back(), "db")->find("latency_mult")->as_number(), 4.0, 1e-9);
  CHECK(sim.command(R"({"op":"clear_faults"})").empty());
  CHECK(sim.command(R"({"op":"scale","service":"api","instances":3})").empty());
  CHECK(sim.command(R"({"op":"policy","service":"api","target":"db","timeout_ms":50,"retry":{"max_attempts":2}})").empty());
  sim.advance_to(30);
  f = json::parse(sim.take_frames());
  CHECK_NEAR(service(f.items().back(), "db")->find("latency_mult")->as_number(), 1.0, 1e-9);
  CHECK(service(f.items().back(), "api")->find("instances")->as_number() == 3);
  CHECK(!sim.command(R"({"op":"scale","service":"nope","instances":3})").empty());
  CHECK(!sim.command(R"({"op":"teleport"})").empty());
  CHECK(!sim.command("not json").empty());
  CHECK(sim.now() >= 30.0 - 1e-9);
}

namespace {

std::string slow_refill_spec(double extend_s) {
  return R"({"seed":3,"horizon_s":260,"warmup_s":30,
    "recovery":{"extend_s":)" + std::to_string(extend_s) + R"(},
    "traffic":{"rps":1500,"entry":"api","client":{"timeout_ms":1000}},
    "services":[
      {"name":"api","instances":4,"concurrency":64,"service_time":{"dist":"lognormal","p50_ms":1,"p99_ms":4},
       "calls":[{"to":"cache","timeout_ms":400}]},
      {"name":"cache","instances":2,"concurrency":128,"service_time":{"dist":"lognormal","p50_ms":0.3,"p99_ms":1},
       "cache":{"hit_ratio":0.95,"key_space":600000,"miss_target":"db"},
       "calls":[{"to":"db","timeout_ms":200,"retry":{"max_attempts":2,"base_ms":10,"jitter":"full"},
                 "budget":{"ratio":0.1,"min_per_s":10}}]},
      {"name":"db","instances":1,"concurrency":8,"queue":{"capacity":1000,"discipline":"adaptive_lifo","timeout_ms":200},
       "service_time":{"dist":"lognormal","p50_ms":8,"p99_ms":25}}],
    "faults":[{"kind":"cache_flush","target":"cache","start_s":60}]})";
}

}

TEST_CASE(slow_but_steady_recovery_is_observed_to_the_end) {
  CHECK(run(slow_refill_spec(0)).verdict == Verdict::Metastable);
  const RunSummary extended = run(slow_refill_spec(900));
  CHECK(extended.verdict == Verdict::Recovered);
  CHECK(extended.recovery_s > 60.0);
}

TEST_CASE(a_genuinely_stuck_run_gets_no_extra_time) {
  std::string spec = storm_spec(1.0, false);
  spec.insert(1, R"("recovery":{"extend_s":900},)");
  Simulator sim(parse_spec_text(spec));
  sim.run();
  CHECK(sim.summary().verdict == Verdict::Metastable);
  CHECK(sim.spec().horizon_s == 200.0);
}
