#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "histogram.hpp"
#include "spec.hpp"

namespace kessler {

inline constexpr const char* kEngineVersion = "0.2.0";

struct ServiceTotals {
  uint64_t arrivals = 0, ok = 0, err = 0, shed = 0, dropped = 0, wasted = 0;
  uint64_t timeouts = 0, retries = 0, cb_rejects = 0, budget_denied = 0;
  Histogram latency;
};

struct ClientTotals {
  uint64_t offered = 0, ok = 0, err = 0, good = 0, retries = 0;
  Histogram latency;
};

struct SeriesPoint {
  double t;
  double offered;
  double good;
  double ok;
  double err;
  double p50;
  double p99;
  double amp;
  double wasted;
};

enum class Verdict : uint8_t { Stable, Recovered, Metastable, Overloaded };
const char* to_string(Verdict v);

struct RunSummary {
  Verdict verdict = Verdict::Stable;
  bool assessable = true;
  double baseline_goodput = 0;
  double baseline_offered = 0;
  double trigger_start = -1;
  double trigger_end = -1;
  double recovery_s = -1;
  double min_goodput_ratio = 1;
  double peak_amp = 0;
  double wasted_fraction = 0;
  double lost_requests = 0;
  double p99_pre = 0, p99_during = 0, p99_post = 0;
  uint64_t events = 0;
  double wall_ms = 0;
  bool truncated = false;
};

class Simulator {
 public:
  explicit Simulator(const Spec& spec, bool emit_frames = false);
  ~Simulator();
  Simulator(const Simulator&) = delete;
  Simulator& operator=(const Simulator&) = delete;

  bool advance_to(double t_s);
  void run();

  bool finished() const;
  double now() const;
  uint64_t events() const;

  std::string take_frames();

  RunSummary summary() const;
  std::string summary_json(bool include_series) const;

  std::string command(std::string_view json_text);

  const ClientTotals& client_totals() const;
  const ServiceTotals& service_totals(int service) const;
  const std::vector<SeriesPoint>& series() const;
  const Spec& spec() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

std::string static_analysis_json(const Spec& spec);

}
