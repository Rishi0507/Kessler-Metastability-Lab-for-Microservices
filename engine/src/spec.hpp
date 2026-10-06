#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "json.hpp"
#include "rng.hpp"

namespace kessler {

struct SpecError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

enum class DistKind : uint8_t { Constant, Exponential, Lognormal, Uniform };

struct Dist {
  DistKind kind = DistKind::Constant;
  double a = 0.001;
  double b = 0.0;

  double sample(Rng& r) const;
  double mean() const;
  double p99() const;
};

enum class Jitter : uint8_t { None, Full, Equal, Decorrelated };

struct RetryPolicy {
  int max_attempts = 1;
  double base_s = 0.05;
  double cap_s = 1.0;
  Jitter jitter = Jitter::Full;
  bool on_timeout = true;
  bool on_error = true;
};

struct BudgetPolicy {
  bool enabled = false;
  double ratio = 0.1;
  double min_per_s = 10.0;
  double max_tokens = 100.0;
};

struct BreakerPolicy {
  bool enabled = false;
  double failure_rate = 0.5;
  double window_s = 10.0;
  int min_requests = 20;
  double open_s = 5.0;
  int half_open_probes = 3;
};

struct CallSpec {
  int target = -1;
  double probability = 1.0;
  double timeout_s = 1.0;
  RetryPolicy retry;
  BudgetPolicy budget;
  BreakerPolicy breaker;
};

enum class QueueDiscipline : uint8_t { Fifo, Lifo, AdaptiveLifo };
enum class LbPolicy : uint8_t { RoundRobin, Random, LeastOutstanding, PowerOfTwo };

struct AutoscalePolicy {
  bool enabled = false;
  int min = 1;
  int max = 1;
  double target = 0.6;
  double interval_s = 15.0;
  double provision_s = 60.0;
  double cooldown_s = 120.0;
};

struct CachePolicy {
  bool enabled = false;
  double hit_ratio = 0.9;
  double key_space = 10000.0;
  int miss_call = -1;
};

struct ServiceSpec {
  std::string name;
  int instances = 1;
  int concurrency = 16;
  int queue_capacity = 1000;
  QueueDiscipline discipline = QueueDiscipline::Fifo;
  double queue_timeout_s = 0.0;
  Dist service_time;
  double error_rate = 0.0;
  LbPolicy lb = LbPolicy::RoundRobin;
  double contention_knee = 1.0;
  double contention_factor = 0.0;
  std::vector<CallSpec> calls;
  AutoscalePolicy autoscale;
  CachePolicy cache;
};

struct ClientSpec {
  double timeout_s = 2.0;
  double slo_s = 1.0;
  RetryPolicy retry;
  BudgetPolicy budget;
  BreakerPolicy breaker;
};

enum class PatternKind : uint8_t { Constant, Step, Diurnal };

struct Pattern {
  PatternKind kind = PatternKind::Constant;
  double at_s = 0.0;
  double factor = 1.0;
  double amplitude = 0.0;
  double period_s = 300.0;
  double value(double t) const;
};

enum class FaultKind : uint8_t { Latency, Errors, Crash, Spike, CacheFlush, CapacityLoss };

struct FaultSpec {
  FaultKind kind = FaultKind::Latency;
  int target = -1;
  double magnitude = 1.0;
  double start_s = 0.0;
  double duration_s = 0.0;
  int count = 0;
};

struct Spec {
  uint64_t seed = 1;
  double horizon_s = 240.0;
  double warmup_s = 30.0;
  double window_s = 1.0;
  double net_latency_s = 0.0005;
  double net_jitter_s = 0.0002;
  bool deadline_propagation = false;
  bool cancel_on_timeout = false;
  double rps = 100.0;
  double load_multiplier = 1.0;
  int entry = 0;
  ClientSpec client;
  Pattern pattern;
  std::vector<ServiceSpec> services;
  std::vector<FaultSpec> faults;
  uint64_t max_events = 400000000ull;
  double recovery_tolerance = 0.1;
  double recovery_hold_s = 10.0;

  int find_service(std::string_view name) const;
};

Spec parse_spec(const json::Value& root);
Spec parse_spec_text(std::string_view text);

FaultSpec parse_fault(const json::Value& v, const Spec& spec, const std::string& path);
RetryPolicy parse_retry(const json::Value& v, const std::string& path);
BudgetPolicy parse_budget(const json::Value& v, const std::string& path);
BreakerPolicy parse_breaker(const json::Value& v, const std::string& path);
bool parse_queue_discipline(std::string_view s, QueueDiscipline* out);
bool parse_lb(std::string_view s, LbPolicy* out);

const char* to_string(FaultKind k);
const char* to_string(Jitter j);
const char* to_string(QueueDiscipline d);
const char* to_string(LbPolicy p);

}
