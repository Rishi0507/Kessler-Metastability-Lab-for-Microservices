#include "spec.hpp"

#include <cmath>
#include <cstdio>
#include <functional>
#include <unordered_map>

namespace kessler {

namespace {

constexpr double kPi = 3.14159265358979323846;

[[noreturn]] void fail(const std::string& path, const std::string& msg) {
  throw SpecError(path + ": " + msg);
}

const json::Value& require(const json::Value& obj, std::string_view key, const std::string& path) {
  const json::Value* v = obj.find(key);
  if (!v) fail(path, "missing required field '" + std::string(key) + "'");
  return *v;
}

double num(const json::Value& obj, std::string_view key, double def, const std::string& path) {
  const json::Value* v = obj.find(key);
  if (!v || v->is_null()) return def;
  if (!v->is_number()) fail(path + "." + std::string(key), "expected a number");
  const double x = v->as_number();
  if (!std::isfinite(x)) fail(path + "." + std::string(key), "must be finite");
  return x;
}

double req_num(const json::Value& obj, std::string_view key, const std::string& path) {
  const json::Value& v = require(obj, key, path);
  if (!v.is_number()) fail(path + "." + std::string(key), "expected a number");
  return v.as_number();
}

bool boolean(const json::Value& obj, std::string_view key, bool def, const std::string& path) {
  const json::Value* v = obj.find(key);
  if (!v || v->is_null()) return def;
  if (!v->is_bool()) fail(path + "." + std::string(key), "expected true/false");
  return v->as_bool();
}

std::string str(const json::Value& obj, std::string_view key, const std::string& def, const std::string& path) {
  const json::Value* v = obj.find(key);
  if (!v || v->is_null()) return def;
  if (!v->is_string()) fail(path + "." + std::string(key), "expected a string");
  return v->as_string();
}

void check_range(double x, double lo, double hi, const std::string& path) {
  if (!(x >= lo && x <= hi)) {
    char buf[128];
    std::snprintf(buf, sizeof buf, "must be between %g and %g (got %g)", lo, hi, x);
    fail(path, buf);
  }
}

void require_object(const json::Value& v, const std::string& path) {
  if (!v.is_object()) fail(path, std::string("expected an object, got ") + json::type_name(v.type()));
}

bool block_enabled(const json::Value* v, const std::string& path) {
  if (!v || v->is_null()) return false;
  if (v->is_bool()) return v->as_bool();
  require_object(*v, path);
  return boolean(*v, "enabled", true, path);
}

Dist parse_dist(const json::Value& v, const std::string& path) {
  require_object(v, path);
  const std::string kind = str(v, "dist", "lognormal", path);
  Dist d;
  if (kind == "constant") {
    const double ms = req_num(v, "ms", path);
    check_range(ms, 0.001, 600000, path + ".ms");
    d.kind = DistKind::Constant;
    d.a = ms / 1000.0;
  } else if (kind == "exponential") {
    const double mean = req_num(v, "mean_ms", path);
    check_range(mean, 0.001, 600000, path + ".mean_ms");
    d.kind = DistKind::Exponential;
    d.a = mean / 1000.0;
  } else if (kind == "lognormal") {
    const double p50 = req_num(v, "p50_ms", path);
    const double p99 = req_num(v, "p99_ms", path);
    check_range(p50, 0.001, 600000, path + ".p50_ms");
    if (p99 < p50) fail(path + ".p99_ms", "must be >= p50_ms");
    check_range(p99, 0.001, 600000, path + ".p99_ms");
    d.kind = DistKind::Lognormal;
    d.a = std::log(p50 / 1000.0);
    d.b = (std::log(p99) - std::log(p50)) / kZ99;
  } else if (kind == "uniform") {
    const double lo = req_num(v, "min_ms", path);
    const double hi = req_num(v, "max_ms", path);
    check_range(lo, 0.0, 600000, path + ".min_ms");
    if (hi < lo) fail(path + ".max_ms", "must be >= min_ms");
    d.kind = DistKind::Uniform;
    d.a = lo / 1000.0;
    d.b = hi / 1000.0;
  } else {
    fail(path + ".dist", "unknown distribution '" + kind + "' (constant|exponential|lognormal|uniform)");
  }
  return d;
}

Pattern parse_pattern(const json::Value* v, const std::string& path) {
  Pattern p;
  if (!v || v->is_null()) return p;
  require_object(*v, path);
  const std::string kind = str(*v, "kind", "constant", path);
  if (kind == "constant") {
    p.kind = PatternKind::Constant;
  } else if (kind == "step") {
    p.kind = PatternKind::Step;
    p.at_s = num(*v, "at_s", 0, path);
    p.factor = num(*v, "factor", 1.0, path);
    check_range(p.factor, 0.0, 100.0, path + ".factor");
  } else if (kind == "diurnal") {
    p.kind = PatternKind::Diurnal;
    p.amplitude = num(*v, "amplitude", 0.3, path);
    p.period_s = num(*v, "period_s", 300, path);
    check_range(p.amplitude, 0.0, 0.95, path + ".amplitude");
    check_range(p.period_s, 1.0, 86400.0, path + ".period_s");
  } else {
    fail(path + ".kind", "unknown pattern '" + kind + "' (constant|step|diurnal)");
  }
  return p;
}

bool parse_jitter(std::string_view s, Jitter* out) {
  if (s == "none") *out = Jitter::None;
  else if (s == "full") *out = Jitter::Full;
  else if (s == "equal") *out = Jitter::Equal;
  else if (s == "decorrelated") *out = Jitter::Decorrelated;
  else return false;
  return true;
}

}

double Dist::sample(Rng& r) const {
  switch (kind) {
    case DistKind::Constant: return a;
    case DistKind::Exponential: return r.exponential(a);
    case DistKind::Lognormal: return r.lognormal(a, b);
    case DistKind::Uniform: return r.uniform(a, b);
  }
  return a;
}

double Dist::mean() const {
  switch (kind) {
    case DistKind::Constant: return a;
    case DistKind::Exponential: return a;
    case DistKind::Lognormal: return std::exp(a + 0.5 * b * b);
    case DistKind::Uniform: return 0.5 * (a + b);
  }
  return a;
}

double Dist::p99() const {
  switch (kind) {
    case DistKind::Constant: return a;
    case DistKind::Exponential: return a * 4.605170185988091;
    case DistKind::Lognormal: return std::exp(a + kZ99 * b);
    case DistKind::Uniform: return a + 0.99 * (b - a);
  }
  return a;
}

double Pattern::value(double t) const {
  switch (kind) {
    case PatternKind::Constant: return 1.0;
    case PatternKind::Step: return t >= at_s ? factor : 1.0;
    case PatternKind::Diurnal: return 1.0 + amplitude * std::sin(2.0 * kPi * t / period_s);
  }
  return 1.0;
}

int Spec::find_service(std::string_view name) const {
  for (size_t i = 0; i < services.size(); ++i) {
    if (services[i].name == name) return static_cast<int>(i);
  }
  return -1;
}

RetryPolicy parse_retry(const json::Value& v, const std::string& path) {
  RetryPolicy r;
  if (v.is_null()) return r;
  require_object(v, path);
  r.max_attempts = static_cast<int>(num(v, "max_attempts", 1, path));
  check_range(r.max_attempts, 1, 10, path + ".max_attempts");
  r.base_s = num(v, "base_ms", 50, path) / 1000.0;
  r.cap_s = num(v, "cap_ms", 1000, path) / 1000.0;
  check_range(r.base_s, 0.0, 600.0, path + ".base_ms");
  check_range(r.cap_s, 0.0, 600.0, path + ".cap_ms");
  if (r.cap_s < r.base_s) r.cap_s = r.base_s;
  const std::string j = str(v, "jitter", "full", path);
  if (!parse_jitter(j, &r.jitter)) fail(path + ".jitter", "unknown jitter '" + j + "' (none|full|equal|decorrelated)");
  r.on_timeout = boolean(v, "on_timeout", true, path);
  r.on_error = boolean(v, "on_error", true, path);
  return r;
}

BudgetPolicy parse_budget(const json::Value& v, const std::string& path) {
  BudgetPolicy b;
  b.enabled = block_enabled(&v, path);
  if (!b.enabled || !v.is_object()) return b;
  b.ratio = num(v, "ratio", 0.1, path);
  b.min_per_s = num(v, "min_per_s", 10, path);
  b.max_tokens = num(v, "max_tokens", 100, path);
  check_range(b.ratio, 0.0, 10.0, path + ".ratio");
  check_range(b.min_per_s, 0.0, 1e6, path + ".min_per_s");
  check_range(b.max_tokens, 1.0, 1e7, path + ".max_tokens");
  return b;
}

BreakerPolicy parse_breaker(const json::Value& v, const std::string& path) {
  BreakerPolicy b;
  b.enabled = block_enabled(&v, path);
  if (!b.enabled || !v.is_object()) return b;
  b.failure_rate = num(v, "failure_rate", 0.5, path);
  b.window_s = num(v, "window_s", 10, path);
  b.min_requests = static_cast<int>(num(v, "min_requests", 20, path));
  b.open_s = num(v, "open_s", 5, path);
  b.half_open_probes = static_cast<int>(num(v, "half_open_probes", 3, path));
  check_range(b.failure_rate, 0.01, 1.0, path + ".failure_rate");
  check_range(b.window_s, 1.0, 120.0, path + ".window_s");
  check_range(b.min_requests, 1, 1000000, path + ".min_requests");
  check_range(b.open_s, 0.1, 600.0, path + ".open_s");
  check_range(b.half_open_probes, 1, 1000, path + ".half_open_probes");
  return b;
}

bool parse_queue_discipline(std::string_view s, QueueDiscipline* out) {
  if (s == "fifo") *out = QueueDiscipline::Fifo;
  else if (s == "lifo") *out = QueueDiscipline::Lifo;
  else if (s == "adaptive_lifo") *out = QueueDiscipline::AdaptiveLifo;
  else return false;
  return true;
}

bool parse_lb(std::string_view s, LbPolicy* out) {
  if (s == "round_robin") *out = LbPolicy::RoundRobin;
  else if (s == "random") *out = LbPolicy::Random;
  else if (s == "least_outstanding") *out = LbPolicy::LeastOutstanding;
  else if (s == "p2c") *out = LbPolicy::PowerOfTwo;
  else return false;
  return true;
}

FaultSpec parse_fault(const json::Value& v, const Spec& spec, const std::string& path) {
  require_object(v, path);
  FaultSpec f;
  const std::string kind = str(v, "kind", "", path);
  if (kind == "latency") f.kind = FaultKind::Latency;
  else if (kind == "errors") f.kind = FaultKind::Errors;
  else if (kind == "crash") f.kind = FaultKind::Crash;
  else if (kind == "spike") f.kind = FaultKind::Spike;
  else if (kind == "cache_flush") f.kind = FaultKind::CacheFlush;
  else if (kind == "capacity_loss") f.kind = FaultKind::CapacityLoss;
  else fail(path + ".kind", "unknown fault '" + kind + "' (latency|errors|crash|spike|cache_flush|capacity_loss)");

  if (f.kind != FaultKind::Spike) {
    const std::string target = str(v, "target", "", path);
    f.target = spec.find_service(target);
    if (f.target < 0) fail(path + ".target", "unknown service '" + target + "'");
    if (f.kind == FaultKind::CacheFlush && !spec.services[static_cast<size_t>(f.target)].cache.enabled) {
      fail(path + ".target", "service '" + target + "' is not a cache");
    }
  }
  f.magnitude = num(v, "magnitude", 1.0, path);
  switch (f.kind) {
    case FaultKind::Latency: check_range(f.magnitude, 1.0, 1000.0, path + ".magnitude"); break;
    case FaultKind::Errors: check_range(f.magnitude, 0.0, 1.0, path + ".magnitude"); break;
    case FaultKind::Spike: check_range(f.magnitude, 0.0, 100.0, path + ".magnitude"); break;
    case FaultKind::CapacityLoss: check_range(f.magnitude, 0.0, 0.99, path + ".magnitude"); break;
    default: break;
  }
  f.start_s = num(v, "start_s", 0.0, path);
  f.duration_s = num(v, "duration_s", 0.0, path);
  f.count = static_cast<int>(num(v, "count", 0, path));
  check_range(f.start_s, 0.0, 86400.0, path + ".start_s");
  check_range(f.duration_s, 0.0, 86400.0, path + ".duration_s");
  check_range(f.count, 0, 100000, path + ".count");
  return f;
}

Spec parse_spec(const json::Value& root) {
  require_object(root, "spec");
  Spec s;
  const std::string P = "spec";
  s.seed = static_cast<uint64_t>(num(root, "seed", 1, P));
  s.horizon_s = num(root, "horizon_s", 240, P);
  s.warmup_s = num(root, "warmup_s", 30, P);
  s.window_s = num(root, "window_s", 1.0, P);
  check_range(s.horizon_s, 1.0, 7200.0, P + ".horizon_s");
  check_range(s.warmup_s, 0.0, s.horizon_s, P + ".warmup_s");
  check_range(s.window_s, 0.05, 60.0, P + ".window_s");

  if (const json::Value* net = root.find("network"); net && !net->is_null()) {
    require_object(*net, P + ".network");
    s.net_latency_s = num(*net, "latency_ms", 0.5, P + ".network") / 1000.0;
    s.net_jitter_s = num(*net, "jitter_ms", 0.2, P + ".network") / 1000.0;
    check_range(s.net_latency_s, 0.0, 10.0, P + ".network.latency_ms");
    check_range(s.net_jitter_s, 0.0, s.net_latency_s + 1e-12, P + ".network.jitter_ms");
  }
  if (const json::Value* res = root.find("resilience"); res && !res->is_null()) {
    require_object(*res, P + ".resilience");
    s.deadline_propagation = boolean(*res, "deadline_propagation", false, P + ".resilience");
    s.cancel_on_timeout = boolean(*res, "cancel_on_timeout", false, P + ".resilience");
  }
  if (const json::Value* lim = root.find("limits"); lim && !lim->is_null()) {
    require_object(*lim, P + ".limits");
    s.max_events = static_cast<uint64_t>(num(*lim, "max_events", 4e8, P + ".limits"));
  }
  if (const json::Value* rec = root.find("recovery"); rec && !rec->is_null()) {
    require_object(*rec, P + ".recovery");
    s.recovery_tolerance = num(*rec, "tolerance", 0.1, P + ".recovery");
    s.recovery_hold_s = num(*rec, "hold_s", 10, P + ".recovery");
    check_range(s.recovery_tolerance, 0.0, 0.9, P + ".recovery.tolerance");
    check_range(s.recovery_hold_s, 0.0, 600.0, P + ".recovery.hold_s");
    s.recovery_extend_s = num(*rec, "extend_s", 0, P + ".recovery");
    check_range(s.recovery_extend_s, 0.0, 3600.0, P + ".recovery.extend_s");
  }

  const json::Value& svcs = require(root, "services", P);
  if (!svcs.is_array() || svcs.items().empty()) fail(P + ".services", "must be a non-empty array");
  if (svcs.items().size() > 64) fail(P + ".services", "at most 64 services are supported");
  std::unordered_map<std::string, int> index;
  for (size_t i = 0; i < svcs.items().size(); ++i) {
    const std::string path = P + ".services[" + std::to_string(i) + "]";
    const json::Value& sv = svcs.items()[i];
    require_object(sv, path);
    ServiceSpec svc;
    svc.name = str(sv, "name", "", path);
    if (svc.name.empty() || svc.name.size() > 64) fail(path + ".name", "must be 1-64 characters");
    if (svc.name == "client") fail(path + ".name", "'client' is reserved");
    if (!index.emplace(svc.name, static_cast<int>(i)).second) fail(path + ".name", "duplicate service '" + svc.name + "'");
    s.services.push_back(std::move(svc));
  }

  for (size_t i = 0; i < svcs.items().size(); ++i) {
    const std::string path = P + ".services[" + std::to_string(i) + "]";
    const json::Value& sv = svcs.items()[i];
    ServiceSpec& svc = s.services[i];
    svc.instances = static_cast<int>(num(sv, "instances", 1, path));
    svc.concurrency = static_cast<int>(num(sv, "concurrency", 16, path));
    check_range(svc.instances, 1, 1000, path + ".instances");
    check_range(svc.concurrency, 1, 100000, path + ".concurrency");
    if (const json::Value* q = sv.find("queue"); q && !q->is_null()) {
      require_object(*q, path + ".queue");
      svc.queue_capacity = static_cast<int>(num(*q, "capacity", 1000, path + ".queue"));
      check_range(svc.queue_capacity, 0, 10000000, path + ".queue.capacity");
      const std::string d = str(*q, "discipline", "fifo", path + ".queue");
      if (!parse_queue_discipline(d, &svc.discipline)) fail(path + ".queue.discipline", "unknown discipline '" + d + "' (fifo|lifo|adaptive_lifo)");
      svc.queue_timeout_s = num(*q, "timeout_ms", 0, path + ".queue") / 1000.0;
      check_range(svc.queue_timeout_s, 0.0, 3600.0, path + ".queue.timeout_ms");
    }
    svc.service_time = parse_dist(require(sv, "service_time", path), path + ".service_time");
    svc.error_rate = num(sv, "error_rate", 0.0, path);
    check_range(svc.error_rate, 0.0, 1.0, path + ".error_rate");
    const std::string lb = str(sv, "lb", "round_robin", path);
    if (!parse_lb(lb, &svc.lb)) fail(path + ".lb", "unknown lb '" + lb + "' (round_robin|random|least_outstanding|p2c)");
    if (const json::Value* c = sv.find("contention"); c && !c->is_null()) {
      require_object(*c, path + ".contention");
      svc.contention_knee = num(*c, "knee", 0.7, path + ".contention");
      svc.contention_factor = num(*c, "factor", 0.0, path + ".contention");
      check_range(svc.contention_knee, 0.0, 0.99, path + ".contention.knee");
      check_range(svc.contention_factor, 0.0, 100.0, path + ".contention.factor");
    }

    if (const json::Value* calls = sv.find("calls"); calls && !calls->is_null()) {
      if (!calls->is_array()) fail(path + ".calls", "must be an array");
      if (calls->items().size() > 32) fail(path + ".calls", "at most 32 calls per service");
      for (size_t j = 0; j < calls->items().size(); ++j) {
        const std::string cp = path + ".calls[" + std::to_string(j) + "]";
        const json::Value& cv = calls->items()[j];
        require_object(cv, cp);
        CallSpec call;
        const std::string to = str(cv, "to", "", cp);
        auto it = index.find(to);
        if (it == index.end()) fail(cp + ".to", "unknown service '" + to + "'");
        if (it->second == static_cast<int>(i)) fail(cp + ".to", "a service cannot call itself");
        call.target = it->second;
        call.probability = num(cv, "probability", 1.0, cp);
        check_range(call.probability, 0.0, 1.0, cp + ".probability");
        call.timeout_s = num(cv, "timeout_ms", 1000, cp) / 1000.0;
        check_range(call.timeout_s, 0.0001, 3600.0, cp + ".timeout_ms");
        if (const json::Value* r = cv.find("retry")) call.retry = parse_retry(*r, cp + ".retry");
        if (const json::Value* b = cv.find("budget")) call.budget = parse_budget(*b, cp + ".budget");
        if (const json::Value* b = cv.find("breaker")) call.breaker = parse_breaker(*b, cp + ".breaker");
        svc.calls.push_back(call);
      }
    }

    if (const json::Value* a = sv.find("autoscale"); block_enabled(a, path + ".autoscale")) {
      AutoscalePolicy& as = svc.autoscale;
      as.enabled = true;
      as.min = static_cast<int>(num(*a, "min", svc.instances, path + ".autoscale"));
      as.max = static_cast<int>(num(*a, "max", svc.instances, path + ".autoscale"));
      as.target = num(*a, "target_utilization", 0.6, path + ".autoscale");
      as.interval_s = num(*a, "interval_s", 15, path + ".autoscale");
      as.provision_s = num(*a, "provision_s", 60, path + ".autoscale");
      as.cooldown_s = num(*a, "cooldown_s", 120, path + ".autoscale");
      check_range(as.min, 1, 1000, path + ".autoscale.min");
      check_range(as.max, as.min, 1000, path + ".autoscale.max");
      check_range(as.target, 0.05, 1.0, path + ".autoscale.target_utilization");
      check_range(as.interval_s, 1.0, 3600.0, path + ".autoscale.interval_s");
      check_range(as.provision_s, 0.0, 3600.0, path + ".autoscale.provision_s");
      check_range(as.cooldown_s, 0.0, 86400.0, path + ".autoscale.cooldown_s");
      if (svc.instances < as.min || svc.instances > as.max) fail(path + ".instances", "must lie within autoscale [min, max]");
    }

    if (const json::Value* c = sv.find("cache"); block_enabled(c, path + ".cache")) {
      CachePolicy& cp = svc.cache;
      cp.enabled = true;
      cp.hit_ratio = num(*c, "hit_ratio", 0.9, path + ".cache");
      cp.key_space = num(*c, "key_space", 10000, path + ".cache");
      check_range(cp.hit_ratio, 0.0, 1.0, path + ".cache.hit_ratio");
      check_range(cp.key_space, 1.0, 1e9, path + ".cache.key_space");
      const std::string miss = str(*c, "miss_target", "", path + ".cache");
      for (size_t j = 0; j < svc.calls.size(); ++j) {
        if (s.services[static_cast<size_t>(svc.calls[j].target)].name == miss) { cp.miss_call = static_cast<int>(j); break; }
      }
      if (cp.miss_call < 0) fail(path + ".cache.miss_target", "must name one of this service's calls");
    }
  }

  {
    std::vector<int> state(s.services.size(), 0);
    std::function<void(int)> dfs = [&](int u) {
      state[static_cast<size_t>(u)] = 1;
      for (const CallSpec& c : s.services[static_cast<size_t>(u)].calls) {
        if (state[static_cast<size_t>(c.target)] == 1) {
          fail(P + ".services", "call graph has a cycle through '" + s.services[static_cast<size_t>(c.target)].name + "'");
        }
        if (state[static_cast<size_t>(c.target)] == 0) dfs(c.target);
      }
      state[static_cast<size_t>(u)] = 2;
    };
    for (size_t i = 0; i < s.services.size(); ++i) {
      if (state[i] == 0) dfs(static_cast<int>(i));
    }
  }

  const json::Value& traffic = require(root, "traffic", P);
  require_object(traffic, P + ".traffic");
  const std::string TP = P + ".traffic";
  s.rps = req_num(traffic, "rps", TP);
  check_range(s.rps, 0.01, 200000.0, TP + ".rps");
  s.load_multiplier = num(traffic, "load_multiplier", 1.0, TP);
  check_range(s.load_multiplier, 0.0, 100.0, TP + ".load_multiplier");
  const std::string entry = str(traffic, "entry", s.services.front().name, TP);
  s.entry = s.find_service(entry);
  if (s.entry < 0) fail(TP + ".entry", "unknown service '" + entry + "'");
  s.pattern = parse_pattern(traffic.find("pattern"), TP + ".pattern");
  if (const json::Value* c = traffic.find("client"); c && !c->is_null()) {
    require_object(*c, TP + ".client");
    s.client.timeout_s = num(*c, "timeout_ms", 2000, TP + ".client") / 1000.0;
    s.client.slo_s = num(*c, "slo_ms", s.client.timeout_s * 1000.0, TP + ".client") / 1000.0;
    check_range(s.client.timeout_s, 0.001, 3600.0, TP + ".client.timeout_ms");
    check_range(s.client.slo_s, 0.001, 3600.0, TP + ".client.slo_ms");
    if (const json::Value* r = c->find("retry")) s.client.retry = parse_retry(*r, TP + ".client.retry");
    if (const json::Value* b = c->find("budget")) s.client.budget = parse_budget(*b, TP + ".client.budget");
    if (const json::Value* b = c->find("breaker")) s.client.breaker = parse_breaker(*b, TP + ".client.breaker");
  }

  if (const json::Value* faults = root.find("faults"); faults && !faults->is_null()) {
    if (!faults->is_array()) fail(P + ".faults", "must be an array");
    if (faults->items().size() > 64) fail(P + ".faults", "at most 64 faults");
    for (size_t i = 0; i < faults->items().size(); ++i) {
      s.faults.push_back(parse_fault(faults->items()[i], s, P + ".faults[" + std::to_string(i) + "]"));
    }
  }
  return s;
}

Spec parse_spec_text(std::string_view text) {
  json::Value root;
  try {
    root = json::parse(text);
  } catch (const json::ParseError& e) {
    throw SpecError(e.what());
  }
  return parse_spec(root);
}

const char* to_string(FaultKind k) {
  switch (k) {
    case FaultKind::Latency: return "latency";
    case FaultKind::Errors: return "errors";
    case FaultKind::Crash: return "crash";
    case FaultKind::Spike: return "spike";
    case FaultKind::CacheFlush: return "cache_flush";
    case FaultKind::CapacityLoss: return "capacity_loss";
  }
  return "unknown";
}

const char* to_string(Jitter j) {
  switch (j) {
    case Jitter::None: return "none";
    case Jitter::Full: return "full";
    case Jitter::Equal: return "equal";
    case Jitter::Decorrelated: return "decorrelated";
  }
  return "unknown";
}

const char* to_string(QueueDiscipline d) {
  switch (d) {
    case QueueDiscipline::Fifo: return "fifo";
    case QueueDiscipline::Lifo: return "lifo";
    case QueueDiscipline::AdaptiveLifo: return "adaptive_lifo";
  }
  return "unknown";
}

const char* to_string(LbPolicy p) {
  switch (p) {
    case LbPolicy::RoundRobin: return "round_robin";
    case LbPolicy::Random: return "random";
    case LbPolicy::LeastOutstanding: return "least_outstanding";
    case LbPolicy::PowerOfTwo: return "p2c";
  }
  return "unknown";
}

}
