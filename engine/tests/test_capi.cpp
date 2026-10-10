#include <cstring>
#include <string>

#include "../include/kessler/kessler.h"
#include "../src/json.hpp"
#include "harness.hpp"

namespace {
constexpr const char* kSpec = R"({"horizon_s":40,"warmup_s":5,"traffic":{"rps":50},
  "services":[{"name":"api","service_time":{"dist":"constant","ms":2}}]})";
}

TEST_CASE(capi_full_lifecycle) {
  char* err = nullptr;
  ks_sim* sim = ks_sim_create(kSpec, std::strlen(kSpec), 1, &err);
  REQUIRE(sim != nullptr);
  CHECK(err == nullptr);
  CHECK(ks_sim_advance(sim, 10.0) == 1);
  CHECK(ks_sim_now(sim) >= 10.0 - 1e-9);
  size_t len = 0;
  char* frames = ks_sim_take_frames(sim, &len);
  REQUIRE(frames != nullptr);
  const kessler::json::Value f = kessler::json::parse(std::string(frames, len));
  CHECK(f.is_array() && f.items().size() == 10);
  ks_free(frames);
  const char* cmd = R"({"op":"load","multiplier":2})";
  CHECK(ks_sim_command(sim, cmd, std::strlen(cmd), &err) == 0);
  ks_sim_run(sim);
  CHECK(ks_sim_finished(sim) == 1);
  CHECK(ks_sim_events(sim) > 0);
  char* summary = ks_sim_summary(sim, 1, &len);
  REQUIRE(summary != nullptr);
  const kessler::json::Value s = kessler::json::parse(std::string(summary, len));
  CHECK(s.find("verdict")->as_string() == "stable");
  CHECK(s.find("series")->find("t")->items().size() == 40);
  ks_free(summary);
  ks_sim_destroy(sim);
}

TEST_CASE(capi_reports_errors_without_throwing) {
  char* err = nullptr;
  const char* bad = R"({"traffic":{"rps":1},"services":[{"name":"a"}]})";
  CHECK(ks_sim_create(bad, std::strlen(bad), 0, &err) == nullptr);
  REQUIRE(err != nullptr);
  CHECK(std::strstr(err, "service_time") != nullptr);
  ks_free(err);
  err = nullptr;
  CHECK(ks_analyze("[]", 2, &err) == nullptr);
  CHECK(err != nullptr);
  ks_free(err);
  CHECK(std::strcmp(ks_version(), "0.1.0") == 0);
}

TEST_CASE(capi_one_shot_run_and_analyze) {
  char* err = nullptr;
  char* out = ks_run(kSpec, std::strlen(kSpec), 0, &err);
  REQUIRE(out != nullptr);
  CHECK(std::strstr(out, "\"verdict\":\"stable\"") != nullptr);
  ks_free(out);
  out = ks_analyze(kSpec, std::strlen(kSpec), &err);
  REQUIRE(out != nullptr);
  CHECK(std::strstr(out, "\"bottleneck\":\"api\"") != nullptr);
  ks_free(out);
}
