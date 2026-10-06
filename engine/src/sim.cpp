#include "sim.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <limits>

#include "json.hpp"

namespace kessler {

namespace {

constexpr uint32_t kNone = 0xFFFFFFFFu;
constexpr double kInf = std::numeric_limits<double>::infinity();
constexpr double kEps = 1e-9;

enum EventType : uint32_t {
  EV_ARRIVAL = 1,
  EV_DELIVER,
  EV_LOCAL_DONE,
  EV_RESPONSE,
  EV_TIMEOUT,
  EV_RETRY,
  EV_WINDOW,
  EV_FAULT_START,
  EV_FAULT_END,
  EV_AUTOSCALE,
  EV_INSTANCE_UP,
  EV_END,
};

struct Event {
  double t;
  uint64_t seq;
  uint32_t type;
  uint32_t a;
  uint32_t b;
  uint32_t gen;
};

struct EventAfter {
  bool operator()(const Event& x, const Event& y) const {
    return x.t > y.t || (x.t == y.t && x.seq > y.seq);
  }
};

enum class Status : uint8_t { Ok, Error, Timeout, Shed, Deadline, CircuitOpen, ConnReset, Cancelled };

enum JobState : uint8_t { JS_FREE, JS_QUEUED, JS_RUNNING, JS_WAITING, JS_BACKOFF };

struct Job {
  uint32_t gen = 0;
  int32_t service = -1;
  int32_t instance = -1;
  uint32_t parent_call = kNone;
  uint32_t parent_gen = 0;
  uint32_t active_call = kNone;
  uint32_t active_gen = 0;
  double arrival_t = 0;
  double start_t = 0;
  double deadline = kInf;
  double work_s = 0;
  double carried_s = 0;
  double prev_backoff = 0;
  int16_t stage = 0;
  int16_t attempt = 0;
  uint8_t state = JS_FREE;
  bool cancelled = false;
  bool holds_slot = false;
};

enum CallState : uint8_t { CS_FREE, CS_PENDING };

struct Call {
  uint32_t gen = 0;
  uint32_t job = kNone;
  uint32_t job_gen = 0;
  uint32_t child = kNone;
  uint32_t child_gen = 0;
  int32_t from = -1;
  int16_t edge = 0;
  int32_t to = -1;
  int32_t inst = -1;
  double deadline = kInf;
  double carried_s = 0;
  uint8_t state = CS_FREE;
  bool probe = false;
  bool counted = false;
};

struct QItem {
  uint32_t id;
  uint32_t gen;
};

struct Instance {
  int busy = 0;
  int outstanding = 0;
  bool alive = true;
  bool draining = false;
  std::deque<QItem> queue;
};

struct Breaker {
  enum State : uint8_t { Closed = 0, Open = 1, HalfOpen = 2 };
  State state = Closed;
  double open_until = 0;
  int ho_inflight = 0;
  int ho_success = 0;
  std::vector<uint32_t> succ, fail;
  uint64_t tot_succ = 0, tot_fail = 0;
  int64_t head_sec = 0;
};

struct EdgeRt {
  Breaker breaker;
  double tokens = 0;
  double refill_t = 0;
  uint32_t w_attempts = 0, w_errors = 0, w_retries = 0, w_timeouts = 0, w_cb = 0, w_budget = 0;
};

struct Window {
  uint32_t arrivals = 0, started = 0, ok = 0, err = 0, shed = 0, dropped = 0, wasted = 0;
  uint32_t timeouts = 0, retries = 0, cb_rejects = 0, budget_denied = 0;
  double busy_area = 0, cap_area = 0, queue_area = 0;
  int queue_max = 0;
  double work_s = 0, wasted_work_s = 0;
  Histogram latency;
};

struct ClientWindow {
  uint32_t offered = 0, attempts = 0, ok = 0, err = 0, good = 0, retries = 0;
  Histogram latency;
};

struct ServiceRt {
  std::vector<Instance> inst;
  std::vector<int> alive;
  int alive_n = 0;
  int pending_up = 0;
  uint32_t rr = 0;
  double latency_mult = 1.0;
  double extra_error = 0.0;
  double capacity_mult = 1.0;
  double fills = 0.0;
  double last_scale_t = -kInf;
  int busy_total = 0;
  int queued_total = 0;
  double last_t = 0;
  double as_busy_area = 0, as_cap_area = 0;
  std::vector<EdgeRt> edges;
  Window w;
  Rng rng;
};

struct FaultRt {
  FaultSpec f;
  bool active = false;
  bool scheduled = false;
  std::vector<int> killed;
};

inline double clamp01(double x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }

}

const char* to_string(Verdict v) {
  switch (v) {
    case Verdict::Stable: return "stable";
    case Verdict::Recovered: return "recovered";
    case Verdict::Metastable: return "metastable";
    case Verdict::Overloaded: return "overloaded";
  }
  return "unknown";
}

struct Simulator::Impl {
  Spec spec;
  bool emit_frames;
  int client_index;

  double now = 0;
  uint64_t seq = 0;
  uint64_t events = 0;
  bool finished = false;
  bool truncated = false;

  std::vector<Event> heap;
  std::deque<Job> jobs;
  std::vector<uint32_t> free_jobs;
  std::deque<Call> calls;
  std::vector<uint32_t> free_calls;

  std::vector<ServiceRt> svc;
  EdgeRt client_edge;
  ClientWindow cw;
  ClientTotals ctot;
  std::vector<ServiceTotals> stot;

  Rng rng_arrival, rng_net, rng_lb, rng_misc;
  uint32_t arrival_gen = 0;
  double traffic_mult = 1.0;

  std::vector<double> visits;
  double visits_total = 1.0;

  std::vector<FaultRt> faults;
  double trigger_start = kInf, trigger_end = -kInf;

  std::string frames;
  bool frames_open = false;
  std::vector<SeriesPoint> series;
  Histogram phase_hist[3];
  double total_work_s = 0, total_wasted_s = 0;

  std::chrono::steady_clock::time_point wall_start;
  double wall_ms = 0;

  explicit Impl(const Spec& s, bool frames_enabled)
      : spec(s),
        emit_frames(frames_enabled),
        client_index(static_cast<int>(s.services.size())),
        rng_arrival(Rng::stream(s.seed, 1)),
        rng_net(Rng::stream(s.seed, 2)),
        rng_lb(Rng::stream(s.seed, 3)),
        rng_misc(Rng::stream(s.seed, 4)) {
    wall_start = std::chrono::steady_clock::now();
    const size_t n = spec.services.size();
    svc.resize(n);
    stot.resize(n);
    for (size_t i = 0; i < n; ++i) {
      const ServiceSpec& ss = spec.services[i];
      ServiceRt& S = svc[i];
      S.inst.resize(static_cast<size_t>(ss.instances));
      S.edges.resize(ss.calls.size());
      S.rng = Rng::stream(spec.seed, 100 + i);
      if (ss.cache.enabled) S.fills = 20.0 * ss.cache.key_space;
      rebuild_alive(static_cast<int>(i));
    }
    compute_visits();

    for (const FaultSpec& f : spec.faults) {
      FaultRt fr;
      fr.f = f;
      fr.scheduled = true;
      faults.push_back(fr);
      trigger_start = std::min(trigger_start, f.start_s);
      trigger_end = std::max(trigger_end, f.start_s + f.duration_s);
    }
    for (uint32_t i = 0; i < faults.size(); ++i) push(faults[i].f.start_s, EV_FAULT_START, i);
    for (size_t i = 0; i < n; ++i) {
      if (spec.services[i].autoscale.enabled) push(spec.services[i].autoscale.interval_s, EV_AUTOSCALE, static_cast<uint32_t>(i));
    }
    push(spec.window_s, EV_WINDOW);
    push(spec.horizon_s + kEps, EV_END);
    reschedule_arrival();
  }

  void push(double t, uint32_t type, uint32_t a = 0, uint32_t b = 0, uint32_t gen = 0) {
    heap.push_back(Event{t, seq++, type, a, b, gen});
    std::push_heap(heap.begin(), heap.end(), EventAfter{});
  }

  uint32_t alloc_job() {
    uint32_t id;
    if (!free_jobs.empty()) {
      id = free_jobs.back();
      free_jobs.pop_back();
    } else {
      id = static_cast<uint32_t>(jobs.size());
      jobs.emplace_back();
    }
    const uint32_t g = jobs[id].gen;
    jobs[id] = Job{};
    jobs[id].gen = g;
    return id;
  }

  void free_job(uint32_t id) {
    Job& j = jobs[id];
    j.state = JS_FREE;
    ++j.gen;
    free_jobs.push_back(id);
  }

  uint32_t alloc_call() {
    uint32_t id;
    if (!free_calls.empty()) {
      id = free_calls.back();
      free_calls.pop_back();
    } else {
      id = static_cast<uint32_t>(calls.size());
      calls.emplace_back();
    }
    const uint32_t g = calls[id].gen;
    calls[id] = Call{};
    calls[id].gen = g;
    return id;
  }

  void release_call(uint32_t id) {
    Call& c = calls[id];
    if (c.counted) {
      --svc[static_cast<size_t>(c.to)].inst[static_cast<size_t>(c.inst)].outstanding;
      c.counted = false;
    }
    c.state = CS_FREE;
    ++c.gen;
    free_calls.push_back(id);
  }

  double net() {
    if (spec.net_jitter_s <= 0) return spec.net_latency_s;
    return std::max(0.0, spec.net_latency_s + spec.net_jitter_s * (2.0 * rng_net.uniform() - 1.0));
  }

  int eff_conc(int s) const {
    const double c = std::floor(spec.services[static_cast<size_t>(s)].concurrency * svc[static_cast<size_t>(s)].capacity_mult);
    return c < 1 ? 1 : static_cast<int>(c);
  }

  void touch(int s) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    const double dt = now - S.last_t;
    if (dt <= 0) return;
    const double cap = static_cast<double>(S.alive_n) * eff_conc(s);
    S.w.busy_area += S.busy_total * dt;
    S.w.queue_area += S.queued_total * dt;
    S.w.cap_area += cap * dt;
    S.as_busy_area += S.busy_total * dt;
    S.as_cap_area += cap * dt;
    S.last_t = now;
  }

  void rebuild_alive(int s) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    S.alive.clear();
    S.alive_n = 0;
    for (size_t i = 0; i < S.inst.size(); ++i) {
      if (S.inst[i].alive) ++S.alive_n;
      if (S.inst[i].alive && !S.inst[i].draining) S.alive.push_back(static_cast<int>(i));
    }
  }

  double hit_ratio(int s) const {
    const CachePolicy& c = spec.services[static_cast<size_t>(s)].cache;
    return c.hit_ratio * (1.0 - std::exp(-svc[static_cast<size_t>(s)].fills / c.key_space));
  }

  void compute_visits() {
    const size_t n = spec.services.size();
    visits.assign(n, 0.0);
    std::vector<int> order;
    std::vector<char> seen(n, 0);
    std::vector<std::pair<int, size_t>> stack;
    stack.emplace_back(spec.entry, 0);
    seen[static_cast<size_t>(spec.entry)] = 1;
    while (!stack.empty()) {
      auto& [u, k] = stack.back();
      const auto& cs = spec.services[static_cast<size_t>(u)].calls;
      if (k < cs.size()) {
        const int v = cs[k++].target;
        if (!seen[static_cast<size_t>(v)]) {
          seen[static_cast<size_t>(v)] = 1;
          stack.emplace_back(v, 0);
        }
      } else {
        order.push_back(u);
        stack.pop_back();
      }
    }
    std::reverse(order.begin(), order.end());
    visits[static_cast<size_t>(spec.entry)] = 1.0;
    for (int u : order) {
      const ServiceSpec& ss = spec.services[static_cast<size_t>(u)];
      for (size_t k = 0; k < ss.calls.size(); ++k) {
        double p = ss.calls[k].probability;
        if (ss.cache.enabled && static_cast<int>(k) == ss.cache.miss_call) p *= 1.0 - ss.cache.hit_ratio;
        visits[static_cast<size_t>(ss.calls[k].target)] += visits[static_cast<size_t>(u)] * p;
      }
    }
    visits_total = 0;
    for (double v : visits) visits_total += v;
    if (visits_total <= 0) visits_total = 1;
  }

  bool is_client(int from) const { return from == client_index; }
  EdgeRt& edge(int from, int idx) { return is_client(from) ? client_edge : svc[static_cast<size_t>(from)].edges[static_cast<size_t>(idx)]; }
  const RetryPolicy& retry_of(int from, int idx) const {
    return is_client(from) ? spec.client.retry : spec.services[static_cast<size_t>(from)].calls[static_cast<size_t>(idx)].retry;
  }
  const BudgetPolicy& budget_of(int from, int idx) const {
    return is_client(from) ? spec.client.budget : spec.services[static_cast<size_t>(from)].calls[static_cast<size_t>(idx)].budget;
  }
  const BreakerPolicy& breaker_of(int from, int idx) const {
    return is_client(from) ? spec.client.breaker : spec.services[static_cast<size_t>(from)].calls[static_cast<size_t>(idx)].breaker;
  }

  void breaker_rotate(Breaker& b, const BreakerPolicy& p) {
    const int64_t sec = static_cast<int64_t>(std::floor(now));
    if (b.succ.empty()) {
      const size_t n = static_cast<size_t>(std::max(1.0, std::ceil(p.window_s)));
      b.succ.assign(n, 0);
      b.fail.assign(n, 0);
      b.head_sec = sec;
      return;
    }
    if (sec <= b.head_sec) return;
    const int64_t n = static_cast<int64_t>(b.succ.size());
    const int64_t steps = std::min<int64_t>(sec - b.head_sec, n);
    for (int64_t k = 1; k <= steps; ++k) {
      const size_t idx = static_cast<size_t>((b.head_sec + k) % n);
      b.tot_succ -= b.succ[idx];
      b.tot_fail -= b.fail[idx];
      b.succ[idx] = 0;
      b.fail[idx] = 0;
    }
    b.head_sec = sec;
  }

  void breaker_trip(Breaker& b, const BreakerPolicy& p) {
    b.state = Breaker::Open;
    b.open_until = now + p.open_s;
    b.ho_inflight = 0;
    b.ho_success = 0;
  }

  void breaker_close(Breaker& b) {
    b.state = Breaker::Closed;
    std::fill(b.succ.begin(), b.succ.end(), 0u);
    std::fill(b.fail.begin(), b.fail.end(), 0u);
    b.tot_succ = b.tot_fail = 0;
    b.head_sec = static_cast<int64_t>(std::floor(now));
  }

  int breaker_allow(Breaker& b, const BreakerPolicy& p) {
    if (!p.enabled) return 1;
    if (b.state == Breaker::Open) {
      if (now < b.open_until) return 0;
      b.state = Breaker::HalfOpen;
      b.ho_inflight = 0;
      b.ho_success = 0;
    }
    if (b.state == Breaker::HalfOpen) {
      if (b.ho_inflight >= p.half_open_probes) return 0;
      ++b.ho_inflight;
      return 2;
    }
    return 1;
  }

  void breaker_release_probe(Breaker& b) {
    if (b.state == Breaker::HalfOpen && b.ho_inflight > 0) --b.ho_inflight;
  }

  void breaker_record(Breaker& b, const BreakerPolicy& p, bool success, bool probe) {
    if (!p.enabled) return;
    if (b.state == Breaker::HalfOpen) {
      if (!probe) return;
      if (b.ho_inflight > 0) --b.ho_inflight;
      if (!success) {
        breaker_trip(b, p);
      } else if (++b.ho_success >= p.half_open_probes) {
        breaker_close(b);
      }
      return;
    }
    if (b.state == Breaker::Open) return;
    breaker_rotate(b, p);
    const size_t idx = static_cast<size_t>(b.head_sec % static_cast<int64_t>(b.succ.size()));
    if (success) {
      ++b.succ[idx];
      ++b.tot_succ;
    } else {
      ++b.fail[idx];
      ++b.tot_fail;
    }
    const uint64_t total = b.tot_succ + b.tot_fail;
    if (total >= static_cast<uint64_t>(p.min_requests) &&
        static_cast<double>(b.tot_fail) >= p.failure_rate * static_cast<double>(total)) {
      breaker_trip(b, p);
    }
  }

  void budget_refill(EdgeRt& e, const BudgetPolicy& p) {
    const double dt = now - e.refill_t;
    if (dt > 0) {
      e.tokens = std::min(p.max_tokens, e.tokens + p.min_per_s * dt);
      e.refill_t = now;
    }
  }

  double backoff(const RetryPolicy& p, Job& J) {
    const double exp = p.base_s * std::ldexp(1.0, std::min(J.attempt - 1, 30));
    const double b = std::min(p.cap_s, exp);
    switch (p.jitter) {
      case Jitter::None: return b;
      case Jitter::Full: return rng_misc.uniform() * b;
      case Jitter::Equal: return 0.5 * b + 0.5 * b * rng_misc.uniform();
      case Jitter::Decorrelated: {
        const double prev = J.prev_backoff > 0 ? J.prev_backoff : p.base_s;
        const double d = std::min(p.cap_s, rng_misc.uniform(p.base_s, std::max(p.base_s, prev * 3.0)));
        J.prev_backoff = d;
        return d;
      }
    }
    return b;
  }

  int pick_instance(int s) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    const uint32_t n = static_cast<uint32_t>(S.alive.size());
    if (n == 0) return -1;
    switch (spec.services[static_cast<size_t>(s)].lb) {
      case LbPolicy::RoundRobin: return S.alive[S.rr++ % n];
      case LbPolicy::Random: return S.alive[rng_lb.below(n)];
      case LbPolicy::LeastOutstanding: {
        const uint32_t start = S.rr++ % n;
        int best = S.alive[start];
        for (uint32_t k = 1; k < n; ++k) {
          const int i = S.alive[(start + k) % n];
          if (S.inst[static_cast<size_t>(i)].outstanding < S.inst[static_cast<size_t>(best)].outstanding) best = i;
        }
        return best;
      }
      case LbPolicy::PowerOfTwo: {
        if (n == 1) return S.alive[0];
        const uint32_t a = rng_lb.below(n);
        uint32_t b = rng_lb.below(n - 1);
        if (b >= a) ++b;
        const int ia = S.alive[a], ib = S.alive[b];
        return S.inst[static_cast<size_t>(ia)].outstanding <= S.inst[static_cast<size_t>(ib)].outstanding ? ia : ib;
      }
    }
    return S.alive[0];
  }

  double current_rate() const { return spec.rps * spec.load_multiplier * traffic_mult * spec.pattern.value(now); }

  void reschedule_arrival() {
    ++arrival_gen;
    const double r = current_rate();
    if (r > 0) push(now + rng_arrival.exponential(1.0 / r), EV_ARRIVAL, 0, 0, arrival_gen);
  }

  void on_arrival(uint32_t gen) {
    if (gen != arrival_gen) return;
    const uint32_t jid = alloc_job();
    Job& J = jobs[jid];
    J.service = client_index;
    J.arrival_t = now;
    J.state = JS_RUNNING;
    ++cw.offered;
    issue_call(jid);
    const double r = current_rate();
    if (r > 0) push(now + rng_arrival.exponential(1.0 / r), EV_ARRIVAL, 0, 0, arrival_gen);
  }

  void issue_call(uint32_t jid) {
    Job& J = jobs[jid];
    const int from = J.service;
    const int idx = J.stage;
    const bool client = is_client(from);
    const int target = client ? spec.entry : spec.services[static_cast<size_t>(from)].calls[static_cast<size_t>(idx)].target;
    const double timeout_cfg = client ? spec.client.timeout_s : spec.services[static_cast<size_t>(from)].calls[static_cast<size_t>(idx)].timeout_s;
    EdgeRt& E = edge(from, idx);

    ++J.attempt;
    ++E.w_attempts;
    if (client) ++cw.attempts;
    if (J.attempt > 1) {
      ++E.w_retries;
      if (client) ++cw.retries;
      else ++svc[static_cast<size_t>(from)].w.retries;
    } else if (const BudgetPolicy& bp = budget_of(from, idx); bp.enabled) {
      budget_refill(E, bp);
      E.tokens = std::min(bp.max_tokens, E.tokens + bp.ratio);
    }

    const int allow = breaker_allow(E.breaker, breaker_of(from, idx));
    if (allow == 0) {
      ++E.w_cb;
      if (!client) ++svc[static_cast<size_t>(from)].w.cb_rejects;
      stage_failed(jid, Status::CircuitOpen, false);
      return;
    }
    double timeout = timeout_cfg;
    if (spec.deadline_propagation && J.deadline < kInf) timeout = std::min(timeout, J.deadline - now);
    if (timeout <= 0) {
      if (allow == 2) breaker_release_probe(E.breaker);
      stage_failed(jid, Status::Deadline, false);
      return;
    }

    const int inst = pick_instance(target);
    const uint32_t cid = alloc_call();
    Call& C = calls[cid];
    C.job = jid;
    C.job_gen = J.gen;
    C.from = from;
    C.edge = static_cast<int16_t>(idx);
    C.to = target;
    C.inst = inst;
    C.deadline = now + timeout;
    C.state = CS_PENDING;
    C.probe = allow == 2;
    J.active_call = cid;
    J.active_gen = C.gen;
    J.state = JS_WAITING;

    const double d = net();
    if (inst < 0) {
      push(now + 2 * d, EV_RESPONSE, cid, static_cast<uint32_t>(Status::ConnReset), C.gen);
      return;
    }
    ++svc[static_cast<size_t>(target)].inst[static_cast<size_t>(inst)].outstanding;
    C.counted = true;
    push(now + d, EV_DELIVER, cid, 0, C.gen);
    push(now + timeout, EV_TIMEOUT, cid, 0, C.gen);
  }

  void respond(uint32_t kid, Status st) {
    const Job& K = jobs[kid];
    if (K.parent_call == kNone) return;
    const Call& P = calls[K.parent_call];
    if (P.gen == K.parent_gen && P.state == CS_PENDING) {
      push(now + net(), EV_RESPONSE, K.parent_call, static_cast<uint32_t>(st), K.parent_gen);
    }
  }

  void on_deliver(uint32_t cid, uint32_t gen) {
    Call& C = calls[cid];
    if (C.gen != gen || C.state != CS_PENDING) return;
    const int s = C.to;
    ServiceRt& S = svc[static_cast<size_t>(s)];
    Instance& I = S.inst[static_cast<size_t>(C.inst)];
    if (!I.alive) {
      push(now + net(), EV_RESPONSE, cid, static_cast<uint32_t>(Status::ConnReset), gen);
      return;
    }
    ++S.w.arrivals;
    const uint32_t kid = alloc_job();
    Job& K = jobs[kid];
    K.service = s;
    K.instance = C.inst;
    K.parent_call = cid;
    K.parent_gen = gen;
    K.arrival_t = now;
    K.deadline = spec.deadline_propagation ? C.deadline : kInf;
    K.state = JS_QUEUED;
    C.child = kid;
    C.child_gen = K.gen;

    if (I.busy < eff_conc(s) && I.queue.empty()) {
      start_job(kid);
      return;
    }
    if (static_cast<int>(I.queue.size()) < spec.services[static_cast<size_t>(s)].queue_capacity) {
      touch(s);
      I.queue.push_back(QItem{kid, K.gen});
      ++S.queued_total;
      S.w.queue_max = std::max(S.w.queue_max, static_cast<int>(I.queue.size()));
      return;
    }
    ++S.w.shed;
    respond(kid, Status::Shed);
    free_job(kid);
  }

  bool start_job(uint32_t kid) {
    Job& K = jobs[kid];
    const int s = K.service;
    const ServiceSpec& ss = spec.services[static_cast<size_t>(s)];
    ServiceRt& S = svc[static_cast<size_t>(s)];
    if (K.cancelled) {
      free_job(kid);
      return false;
    }
    if (spec.deadline_propagation && now >= K.deadline) {
      ++S.w.dropped;
      respond(kid, Status::Deadline);
      free_job(kid);
      return false;
    }
    if (ss.queue_timeout_s > 0 && now - K.arrival_t > ss.queue_timeout_s) {
      ++S.w.dropped;
      respond(kid, Status::Shed);
      free_job(kid);
      return false;
    }
    Instance& I = S.inst[static_cast<size_t>(K.instance)];
    touch(s);
    ++I.busy;
    ++S.busy_total;
    K.holds_slot = true;
    K.state = JS_RUNNING;
    K.start_t = now;
    ++S.w.started;
    double st = ss.service_time.sample(S.rng) * S.latency_mult;
    if (ss.contention_factor > 0) {
      const double u = static_cast<double>(I.busy) / eff_conc(s);
      if (u > ss.contention_knee) {
        st *= 1.0 + ss.contention_factor * (std::min(u, 1.5) - ss.contention_knee) / (1.0 - ss.contention_knee);
      }
    }
    K.work_s += st;
    push(now + st, EV_LOCAL_DONE, kid, 0, K.gen);
    return true;
  }

  void on_local_done(uint32_t kid, uint32_t gen) {
    Job& K = jobs[kid];
    if (K.gen != gen || K.state != JS_RUNNING) return;
    const int s = K.service;
    ServiceRt& S = svc[static_cast<size_t>(s)];
    if (K.cancelled) {
      finish_job(kid, Status::Cancelled);
      return;
    }
    const double p = spec.services[static_cast<size_t>(s)].error_rate + S.extra_error;
    if (p > 0 && S.rng.uniform() < p) {
      finish_job(kid, Status::Error);
      return;
    }
    advance(kid);
  }

  void advance(uint32_t kid) {
    Job& K = jobs[kid];
    const int s = K.service;
    const ServiceSpec& ss = spec.services[static_cast<size_t>(s)];
    while (K.stage < static_cast<int>(ss.calls.size())) {
      double p = ss.calls[static_cast<size_t>(K.stage)].probability;
      if (ss.cache.enabled && K.stage == ss.cache.miss_call) p *= 1.0 - hit_ratio(s);
      if (p >= 1.0 || (p > 0.0 && svc[static_cast<size_t>(s)].rng.uniform() < p)) {
        K.attempt = 0;
        K.prev_backoff = 0;
        issue_call(kid);
        return;
      }
      ++K.stage;
    }
    finish_job(kid, Status::Ok);
  }

  void on_response(uint32_t cid, Status st, uint32_t gen) {
    Call& C = calls[cid];
    if (C.gen != gen || C.state != CS_PENDING) return;
    const uint32_t jid = C.job, jgen = C.job_gen;
    const int from = C.from, idx = C.edge;
    const bool probe = C.probe;
    const double carried = C.carried_s;
    release_call(cid);
    EdgeRt& E = edge(from, idx);
    const bool ok = st == Status::Ok;
    breaker_record(E.breaker, breaker_of(from, idx), ok, probe);
    if (!ok) ++E.w_errors;
    Job& J = jobs[jid];
    if (J.gen != jgen || J.state != JS_WAITING) return;
    J.active_call = kNone;
    J.carried_s += carried;
    if (ok) {
      if (is_client(from)) {
        finish_job(jid, Status::Ok);
      } else {
        const ServiceSpec& ss = spec.services[static_cast<size_t>(from)];
        if (ss.cache.enabled && idx == ss.cache.miss_call) svc[static_cast<size_t>(from)].fills += 1.0;
        ++J.stage;
        advance(jid);
      }
      return;
    }
    const RetryPolicy& rp = retry_of(from, idx);
    const bool retryable = st == Status::Timeout ? rp.on_timeout
                           : (st == Status::Deadline || st == Status::CircuitOpen || st == Status::Cancelled) ? false
                                                                                                                : rp.on_error;
    stage_failed(jid, st, retryable);
  }

  void on_timeout(uint32_t cid, uint32_t gen) {
    Call& C = calls[cid];
    if (C.gen != gen || C.state != CS_PENDING) return;
    const uint32_t jid = C.job, jgen = C.job_gen;
    const int from = C.from, idx = C.edge;
    const bool probe = C.probe;
    const uint32_t child = C.child, child_gen = C.child_gen;
    release_call(cid);
    EdgeRt& E = edge(from, idx);
    ++E.w_timeouts;
    ++E.w_errors;
    if (!is_client(from)) ++svc[static_cast<size_t>(from)].w.timeouts;
    breaker_record(E.breaker, breaker_of(from, idx), false, probe);
    if (spec.cancel_on_timeout && child != kNone) cancel_job(child, child_gen);
    Job& J = jobs[jid];
    if (J.gen != jgen || J.state != JS_WAITING) return;
    J.active_call = kNone;
    stage_failed(jid, Status::Timeout, retry_of(from, idx).on_timeout);
  }

  void stage_failed(uint32_t jid, Status st, bool retryable) {
    Job& J = jobs[jid];
    const int from = J.service, idx = J.stage;
    const RetryPolicy& rp = retry_of(from, idx);
    if (retryable && !J.cancelled && J.attempt < rp.max_attempts) {
      EdgeRt& E = edge(from, idx);
      const BudgetPolicy& bp = budget_of(from, idx);
      bool allowed = true;
      if (bp.enabled) {
        budget_refill(E, bp);
        if (E.tokens >= 1.0) E.tokens -= 1.0;
        else allowed = false;
      }
      if (!allowed) {
        ++E.w_budget;
        if (!is_client(from)) ++svc[static_cast<size_t>(from)].w.budget_denied;
      } else {
        const double delay = backoff(rp, J);
        if (!(spec.deadline_propagation && now + delay >= J.deadline)) {
          J.state = JS_BACKOFF;
          push(now + delay, EV_RETRY, jid, 0, J.gen);
          return;
        }
      }
    }
    finish_job(jid, is_client(from) ? st : Status::Error);
  }

  void on_retry(uint32_t jid, uint32_t gen) {
    Job& J = jobs[jid];
    if (J.gen != gen || J.state != JS_BACKOFF) return;
    if (J.cancelled) {
      finish_job(jid, Status::Cancelled);
      return;
    }
    issue_call(jid);
  }

  void finish_job(uint32_t jid, Status st) {
    Job& J = jobs[jid];
    if (is_client(J.service)) {
      if (st == Status::Ok) {
        const double lat = now - J.arrival_t;
        ++cw.ok;
        cw.latency.record(lat * 1000.0);
        if (lat <= spec.client.slo_s) ++cw.good;
      } else {
        ++cw.err;
      }
      free_job(jid);
      return;
    }
    const int s = J.service;
    ServiceRt& S = svc[static_cast<size_t>(s)];
    const int inst = J.instance;
    if (J.holds_slot) {
      touch(s);
      --S.inst[static_cast<size_t>(inst)].busy;
      --S.busy_total;
      J.holds_slot = false;
    }
    bool waiting = false;
    if (J.parent_call != kNone) {
      const Call& P = calls[J.parent_call];
      waiting = P.gen == J.parent_gen && P.state == CS_PENDING;
    }
    S.w.work_s += J.work_s;
    total_work_s += J.work_s;
    const double subtree = J.work_s + J.carried_s;
    if (st == Status::Ok) {
      ++S.w.ok;
      S.w.latency.record((now - J.arrival_t) * 1000.0);
    } else if (st != Status::Cancelled) {
      ++S.w.err;
    }
    if (!waiting) {
      ++S.w.wasted;
      S.w.wasted_work_s += subtree;
      total_wasted_s += subtree;
    } else {
      calls[J.parent_call].carried_s += subtree;
      push(now + net(), EV_RESPONSE, J.parent_call, static_cast<uint32_t>(st == Status::Ok ? Status::Ok : Status::Error), J.parent_gen);
    }
    free_job(jid);
    dispatch(s, inst);
  }

  void dispatch(int s, int inst) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    Instance& I = S.inst[static_cast<size_t>(inst)];
    const ServiceSpec& ss = spec.services[static_cast<size_t>(s)];
    const int conc = eff_conc(s);
    while (I.alive && I.busy < conc && !I.queue.empty()) {
      touch(s);
      const bool lifo = ss.discipline == QueueDiscipline::Lifo ||
                        (ss.discipline == QueueDiscipline::AdaptiveLifo && static_cast<int>(I.queue.size()) > conc);
      QItem q;
      if (lifo) {
        q = I.queue.back();
        I.queue.pop_back();
      } else {
        q = I.queue.front();
        I.queue.pop_front();
      }
      --S.queued_total;
      const Job& K = jobs[q.id];
      if (K.gen != q.gen || K.state != JS_QUEUED) continue;
      start_job(q.id);
    }
    if (I.draining && I.busy == 0 && I.queue.empty() && I.alive) {
      touch(s);
      I.alive = false;
      I.draining = false;
      rebuild_alive(s);
    }
  }

  void cancel_job(uint32_t kid, uint32_t gen) {
    Job& K = jobs[kid];
    if (K.gen != gen || K.state == JS_FREE) return;
    K.cancelled = true;
    switch (K.state) {
      case JS_QUEUED:
      case JS_RUNNING:
        return;
      case JS_WAITING: {
        if (K.active_call != kNone) {
          Call& C = calls[K.active_call];
          if (C.gen == K.active_gen && C.state == CS_PENDING) {
            const uint32_t child = C.child, child_gen = C.child_gen;
            if (C.probe) breaker_release_probe(edge(C.from, C.edge).breaker);
            release_call(K.active_call);
            K.active_call = kNone;
            if (child != kNone) cancel_job(child, child_gen);
          }
        }
        finish_job(kid, Status::Cancelled);
        return;
      }
      case JS_BACKOFF:
        finish_job(kid, Status::Cancelled);
        return;
      default:
        return;
    }
  }

  void recompute_effects() {
    for (size_t s = 0; s < svc.size(); ++s) touch(static_cast<int>(s));
    for (ServiceRt& S : svc) {
      S.latency_mult = 1.0;
      S.extra_error = 0.0;
      S.capacity_mult = 1.0;
    }
    double tm = 1.0;
    for (const FaultRt& fr : faults) {
      if (!fr.active) continue;
      const FaultSpec& f = fr.f;
      switch (f.kind) {
        case FaultKind::Latency: svc[static_cast<size_t>(f.target)].latency_mult *= f.magnitude; break;
        case FaultKind::Errors: svc[static_cast<size_t>(f.target)].extra_error += f.magnitude; break;
        case FaultKind::CapacityLoss: svc[static_cast<size_t>(f.target)].capacity_mult *= 1.0 - f.magnitude; break;
        case FaultKind::Spike: tm *= f.magnitude; break;
        default: break;
      }
    }
    for (ServiceRt& S : svc) S.extra_error = std::min(1.0, S.extra_error);
    if (tm != traffic_mult) {
      traffic_mult = tm;
      reschedule_arrival();
    }
    for (size_t s = 0; s < svc.size(); ++s) {
      for (size_t i = 0; i < svc[s].inst.size(); ++i) dispatch(static_cast<int>(s), static_cast<int>(i));
    }
  }

  void crash(FaultRt& fr) {
    const int s = fr.f.target;
    ServiceRt& S = svc[static_cast<size_t>(s)];
    touch(s);
    std::vector<int> victims;
    for (size_t i = 0; i < S.inst.size(); ++i) {
      if (S.inst[i].alive) victims.push_back(static_cast<int>(i));
    }
    if (fr.f.count > 0 && static_cast<int>(victims.size()) > fr.f.count) victims.resize(static_cast<size_t>(fr.f.count));
    for (int i : victims) {
      Instance& I = S.inst[static_cast<size_t>(i)];
      I.alive = false;
      I.draining = false;
      for (const QItem& q : I.queue) {
        const Job& K = jobs[q.id];
        if (K.gen == q.gen && K.state == JS_QUEUED) {
          respond(q.id, Status::ConnReset);
          free_job(q.id);
        }
      }
      S.queued_total -= static_cast<int>(I.queue.size());
      I.queue.clear();
    }
    for (uint32_t id = 0; id < jobs.size(); ++id) {
      Job& K = jobs[id];
      if (K.state == JS_FREE || K.service != s || !K.holds_slot) continue;
      if (std::find(victims.begin(), victims.end(), K.instance) == victims.end()) continue;
      if (K.state == JS_WAITING && K.active_call != kNone) {
        Call& C = calls[K.active_call];
        if (C.gen == K.active_gen && C.state == CS_PENDING) {
          if (C.probe) breaker_release_probe(edge(C.from, C.edge).breaker);
          release_call(K.active_call);
        }
        K.active_call = kNone;
      }
      respond(id, Status::ConnReset);
      --S.inst[static_cast<size_t>(K.instance)].busy;
      --S.busy_total;
      K.holds_slot = false;
      ++S.w.err;
      free_job(id);
    }
    fr.killed = victims;
    rebuild_alive(s);
  }

  void on_fault_start(uint32_t fid) {
    FaultRt& fr = faults[fid];
    if (fr.active) return;
    const FaultSpec f = fr.f;
    if (f.kind == FaultKind::CacheFlush) {
      svc[static_cast<size_t>(f.target)].fills = 0.0;
      return;
    }
    fr.active = true;
    if (f.kind == FaultKind::Crash) crash(fr);
    else recompute_effects();
    if (f.duration_s > 0) push(now + f.duration_s, EV_FAULT_END, fid);
  }

  void on_fault_end(uint32_t fid) {
    FaultRt& fr = faults[fid];
    if (!fr.active) return;
    fr.active = false;
    if (fr.f.kind == FaultKind::Crash) {
      const int s = fr.f.target;
      touch(s);
      for (int i : fr.killed) {
        Instance& I = svc[static_cast<size_t>(s)].inst[static_cast<size_t>(i)];
        I.alive = true;
        I.draining = false;
      }
      fr.killed.clear();
      rebuild_alive(s);
    } else {
      recompute_effects();
    }
  }

  void on_autoscale(int s) {
    const AutoscalePolicy& as = spec.services[static_cast<size_t>(s)].autoscale;
    ServiceRt& S = svc[static_cast<size_t>(s)];
    touch(s);
    const double util = S.as_cap_area > 0 ? S.as_busy_area / S.as_cap_area : 0.0;
    S.as_busy_area = S.as_cap_area = 0;
    const int current = static_cast<int>(S.alive.size());
    int desired = static_cast<int>(std::ceil(current * util / as.target - 1e-9));
    desired = std::clamp(desired, as.min, as.max);
    if (desired > current + S.pending_up) {
      const int add = desired - current - S.pending_up;
      S.pending_up += add;
      for (int k = 0; k < add; ++k) push(now + as.provision_s, EV_INSTANCE_UP, static_cast<uint32_t>(s));
      S.last_scale_t = now;
    } else if (desired < current && S.pending_up == 0 && now - S.last_scale_t >= as.cooldown_s) {
      int remove = current - desired;
      std::vector<int> cand = S.alive;
      std::sort(cand.begin(), cand.end(), [&](int a, int b) {
        return S.inst[static_cast<size_t>(a)].outstanding < S.inst[static_cast<size_t>(b)].outstanding;
      });
      for (int i : cand) {
        if (remove-- <= 0) break;
        S.inst[static_cast<size_t>(i)].draining = true;
      }
      rebuild_alive(s);
      for (int i : cand) dispatch(s, i);
      S.last_scale_t = now;
    }
    push(now + as.interval_s, EV_AUTOSCALE, static_cast<uint32_t>(s));
  }

  void add_instance(int s) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    touch(s);
    for (Instance& I : S.inst) {
      if (!I.alive && I.busy == 0 && I.queue.empty() && !is_killed(s, &I - S.inst.data())) {
        I.alive = true;
        I.draining = false;
        rebuild_alive(s);
        return;
      }
    }
    S.inst.emplace_back();
    rebuild_alive(s);
  }

  bool is_killed(int s, ptrdiff_t idx) const {
    for (const FaultRt& fr : faults) {
      if (fr.active && fr.f.kind == FaultKind::Crash && fr.f.target == s &&
          std::find(fr.killed.begin(), fr.killed.end(), static_cast<int>(idx)) != fr.killed.end()) {
        return true;
      }
    }
    return false;
  }

  void on_instance_up(int s) {
    ServiceRt& S = svc[static_cast<size_t>(s)];
    if (S.pending_up > 0) --S.pending_up;
    add_instance(s);
  }

  int phase_of(double t0) const {
    if (trigger_start == kInf || t0 < trigger_start) return 0;
    if (t0 < trigger_end) return 1;
    return 2;
  }

  static const char* breaker_name(const Breaker& b) {
    switch (b.state) {
      case Breaker::Closed: return "closed";
      case Breaker::Open: return "open";
      case Breaker::HalfOpen: return "half_open";
    }
    return "closed";
  }

  void write_edge(json::Writer& w, const char* from, const char* to, EdgeRt& E, const BreakerPolicy& bp, double inv) {
    w.begin_object();
    w.key("from").string(from);
    w.key("to").string(to);
    w.key("rps").number(E.w_attempts * inv, 1);
    w.key("err").number(E.w_attempts ? static_cast<double>(E.w_errors) / E.w_attempts : 0.0, 4);
    w.key("retries").number(E.w_retries * inv, 1);
    w.key("timeouts").number(E.w_timeouts * inv, 1);
    w.key("cb_rejects").number(E.w_cb * inv, 1);
    w.key("budget_denied").number(E.w_budget * inv, 1);
    w.key("breaker").string(bp.enabled ? breaker_name(E.breaker) : "none");
    w.end_object();
  }

  void on_window() {
    const double t = now;
    const double t0 = t - spec.window_s;
    const double inv = 1.0 / spec.window_s;
    for (size_t s = 0; s < svc.size(); ++s) touch(static_cast<int>(s));

    double arrivals = 0, work = 0, wasted = 0;
    for (const ServiceRt& S : svc) {
      arrivals += S.w.arrivals;
      work += S.w.work_s;
      wasted += S.w.wasted_work_s;
    }
    const double amp = cw.offered ? arrivals / (static_cast<double>(cw.offered) * visits_total) : 0.0;
    const double wasted_frac = work > 0 ? std::min(1.0, wasted / work) : 0.0;

    SeriesPoint p{};
    p.t = t;
    p.offered = cw.offered * inv;
    p.good = cw.good * inv;
    p.ok = cw.ok * inv;
    p.err = cw.err * inv;
    p.p50 = cw.latency.percentile(0.50);
    p.p99 = cw.latency.percentile(0.99);
    p.amp = amp;
    p.wasted = wasted_frac;
    series.push_back(p);

    if (emit_frames) {
      json::Writer w;
      w.begin_object();
      w.key("t").number(t, 3);
      w.key("rate").number(current_rate(), 1);
      w.key("client").begin_object();
      w.key("offered").number(p.offered, 1);
      w.key("ok").number(p.ok, 1);
      w.key("err").number(p.err, 1);
      w.key("good").number(p.good, 1);
      w.key("retries").number(cw.retries * inv, 1);
      w.key("p50").number(p.p50, 2);
      w.key("p99").number(p.p99, 2);
      w.end_object();
      w.key("amp").number(amp, 3);
      w.key("wasted").number(wasted_frac, 4);
      w.key("services").begin_array();
      for (size_t s = 0; s < svc.size(); ++s) {
        const ServiceRt& S = svc[s];
        const ServiceSpec& ss = spec.services[s];
        w.begin_object();
        w.key("name").string(ss.name);
        w.key("in").number(S.w.arrivals * inv, 1);
        w.key("ok").number(S.w.ok * inv, 1);
        w.key("err").number(S.w.err * inv, 1);
        w.key("shed").number(S.w.shed * inv, 1);
        w.key("dropped").number(S.w.dropped * inv, 1);
        w.key("timeouts").number(S.w.timeouts * inv, 1);
        w.key("retries").number(S.w.retries * inv, 1);
        w.key("wasted").number(S.w.wasted * inv, 1);
        w.key("util").number(S.w.cap_area > 0 ? clamp01(S.w.busy_area / S.w.cap_area) : 0.0, 3);
        w.key("queue").number(S.w.queue_area * inv, 1);
        w.key("queue_max").integer(S.w.queue_max);
        w.key("p50").number(S.w.latency.percentile(0.50), 2);
        w.key("p99").number(S.w.latency.percentile(0.99), 2);
        w.key("instances").integer(static_cast<int64_t>(S.alive.size()));
        w.key("pending").integer(S.pending_up);
        if (ss.cache.enabled) w.key("hit_ratio").number(hit_ratio(static_cast<int>(s)), 3);
        w.key("latency_mult").number(S.latency_mult, 2);
        w.end_object();
      }
      w.end_array();
      w.key("edges").begin_array();
      write_edge(w, "client", spec.services[static_cast<size_t>(spec.entry)].name.c_str(), client_edge, spec.client.breaker, inv);
      for (size_t s = 0; s < svc.size(); ++s) {
        for (size_t k = 0; k < spec.services[s].calls.size(); ++k) {
          const CallSpec& c = spec.services[s].calls[k];
          write_edge(w, spec.services[s].name.c_str(), spec.services[static_cast<size_t>(c.target)].name.c_str(), svc[s].edges[k], c.breaker, inv);
        }
      }
      w.end_array();
      w.key("faults").begin_array();
      for (const FaultRt& fr : faults) {
        if (!fr.active) continue;
        w.begin_object();
        w.key("kind").string(to_string(fr.f.kind));
        if (fr.f.target >= 0) w.key("target").string(spec.services[static_cast<size_t>(fr.f.target)].name);
        w.key("magnitude").number(fr.f.magnitude, 3);
        w.end_object();
      }
      w.end_array();
      w.end_object();
      frames.append(frames_open ? "," : "[");
      frames_open = true;
      frames.append(w.str());
    }

    if (t0 >= spec.warmup_s - kEps) {
      ctot.offered += cw.offered;
      ctot.ok += cw.ok;
      ctot.err += cw.err;
      ctot.good += cw.good;
      ctot.retries += cw.retries;
      ctot.latency.merge(cw.latency);
      phase_hist[phase_of(t0)].merge(cw.latency);
      for (size_t s = 0; s < svc.size(); ++s) {
        const Window& W = svc[s].w;
        ServiceTotals& T = stot[s];
        T.arrivals += W.arrivals;
        T.ok += W.ok;
        T.err += W.err;
        T.shed += W.shed;
        T.dropped += W.dropped;
        T.wasted += W.wasted;
        T.timeouts += W.timeouts;
        T.retries += W.retries;
        T.cb_rejects += W.cb_rejects;
        T.budget_denied += W.budget_denied;
        T.latency.merge(W.latency);
      }
    }

    cw = ClientWindow{};
    client_edge.w_attempts = client_edge.w_errors = client_edge.w_retries = 0;
    client_edge.w_timeouts = client_edge.w_cb = client_edge.w_budget = 0;
    for (ServiceRt& S : svc) {
      S.w = Window{};
      for (EdgeRt& E : S.edges) E.w_attempts = E.w_errors = E.w_retries = E.w_timeouts = E.w_cb = E.w_budget = 0;
    }

    if (spec.pattern.kind != PatternKind::Constant) reschedule_arrival();
    if (t + spec.window_s <= spec.horizon_s + kEps) push(t + spec.window_s, EV_WINDOW);
  }

  void finish() {
    if (finished) return;
    finished = true;
    wall_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - wall_start).count();
  }

  void handle(const Event& e) {
    switch (e.type) {
      case EV_ARRIVAL: on_arrival(e.gen); break;
      case EV_DELIVER: on_deliver(e.a, e.gen); break;
      case EV_LOCAL_DONE: on_local_done(e.a, e.gen); break;
      case EV_RESPONSE: on_response(e.a, static_cast<Status>(e.b), e.gen); break;
      case EV_TIMEOUT: on_timeout(e.a, e.gen); break;
      case EV_RETRY: on_retry(e.a, e.gen); break;
      case EV_WINDOW: on_window(); break;
      case EV_FAULT_START: on_fault_start(e.a); break;
      case EV_FAULT_END: on_fault_end(e.a); break;
      case EV_AUTOSCALE: on_autoscale(static_cast<int>(e.a)); break;
      case EV_INSTANCE_UP: on_instance_up(static_cast<int>(e.a)); break;
      case EV_END: finish(); break;
      default: break;
    }
  }

  bool advance_to(double t) {
    while (!finished && !heap.empty() && heap.front().t <= t) {
      std::pop_heap(heap.begin(), heap.end(), EventAfter{});
      const Event e = heap.back();
      heap.pop_back();
      now = e.t;
      ++events;
      handle(e);
      if (events >= spec.max_events) {
        truncated = true;
        finish();
      }
    }
    if (!finished && t > now) now = std::min(t, spec.horizon_s);
    return !finished;
  }

  RunSummary summarize() const {
    RunSummary r;
    r.events = events;
    r.wall_ms = finished ? wall_ms
                         : std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - wall_start).count();
    r.truncated = truncated;
    r.wasted_fraction = total_work_s > 0 ? std::min(1.0, total_wasted_s / total_work_s) : 0.0;
    auto p99_or_nan = [](const Histogram& h) { return h.count() ? h.percentile(0.99) : std::nan(""); };
    r.p99_pre = p99_or_nan(phase_hist[0]);
    r.p99_during = p99_or_nan(phase_hist[1]);
    r.p99_post = p99_or_nan(phase_hist[2]);
    const bool has_trigger = trigger_start != kInf;
    if (has_trigger) {
      r.trigger_start = trigger_start;
      r.trigger_end = trigger_end;
    }
    const double ws = spec.window_s;
    const size_t n = series.size();
    if (n == 0) return r;

    auto in_baseline = [&](const SeriesPoint& p, bool require_warm) {
      const double t0 = p.t - ws;
      return (!require_warm || t0 >= spec.warmup_s - kEps) && (!has_trigger || p.t <= trigger_start + kEps);
    };
    double sg = 0, so = 0;
    int nb = 0;
    for (const SeriesPoint& p : series) {
      if (in_baseline(p, true)) {
        sg += p.good;
        so += p.offered;
        ++nb;
      }
    }
    if (nb == 0) {
      for (const SeriesPoint& p : series) {
        if (in_baseline(p, false)) {
          sg += p.good;
          so += p.offered;
          ++nb;
        }
      }
    }
    if (nb > 0) {
      r.baseline_goodput = sg / nb;
      r.baseline_offered = so / nb;
    }
    const double base_ratio = so > 0 ? sg / so : 1.0;

    std::vector<double> ratio(n);
    for (size_t i = 0; i < n; ++i) {
      const size_t lo = i > 0 ? i - 1 : 0, hi = std::min(n - 1, i + 1);
      double g = 0, o = 0;
      for (size_t j = lo; j <= hi; ++j) {
        g += series[j].good;
        o += series[j].offered;
      }
      ratio[i] = o > 0 ? std::min(1.0, g / o) : 1.0;
    }

    for (const SeriesPoint& p : series) {
      if (!has_trigger || p.t - ws + kEps >= trigger_start) r.peak_amp = std::max(r.peak_amp, p.amp);
    }
    if (base_ratio < 0.9) {
      r.verdict = Verdict::Overloaded;
      r.min_goodput_ratio = base_ratio;
      return r;
    }
    if (!has_trigger) {
      r.verdict = Verdict::Stable;
      return r;
    }

    const double thresh = (1.0 - spec.recovery_tolerance) * base_ratio;
    bool degraded = false;
    double min_ratio = 1.0;
    for (size_t i = 0; i < n; ++i) {
      if (series[i].t - ws + kEps < trigger_start) continue;
      min_ratio = std::min(min_ratio, ratio[i] / base_ratio);
      if (ratio[i] < thresh) degraded = true;
    }
    r.min_goodput_ratio = std::max(0.0, min_ratio);

    const size_t hold = static_cast<size_t>(std::ceil(spec.recovery_hold_s / ws - kEps));
    double recovered_at = -1;
    for (size_t i = 0; i < n; ++i) {
      const double t0 = series[i].t - ws;
      if (t0 + kEps < trigger_end) continue;
      if (i + hold > n) break;
      bool ok = true;
      for (size_t j = i; j < i + hold && ok; ++j) ok = ratio[j] >= thresh;
      if (ok) {
        recovered_at = t0;
        break;
      }
    }
    const double lost_until = recovered_at >= 0 ? recovered_at : series.back().t;
    for (size_t i = 0; i < n; ++i) {
      const double t0 = series[i].t - ws;
      if (t0 + kEps < trigger_start || t0 >= lost_until - kEps) continue;
      r.lost_requests += std::max(0.0, base_ratio * series[i].offered - series[i].good) * ws;
    }
    r.assessable = trigger_end + spec.recovery_hold_s <= spec.horizon_s + kEps;
    if (!degraded) {
      r.verdict = Verdict::Stable;
      r.recovery_s = 0;
    } else if (recovered_at >= 0) {
      r.verdict = Verdict::Recovered;
      r.recovery_s = std::max(0.0, recovered_at - trigger_end);
    } else {
      r.verdict = Verdict::Metastable;
    }
    return r;
  }

  std::string summary_json(bool include_series) const {
    const RunSummary r = summarize();
    json::Writer w;
    w.begin_object();
    w.key("engine").string(kEngineVersion);
    w.key("seed").integer(static_cast<int64_t>(spec.seed));
    w.key("verdict").string(to_string(r.verdict));
    w.key("assessable").boolean(r.assessable);
    w.key("truncated").boolean(r.truncated);
    w.key("sim_time_s").number(now, 3);
    w.key("events").integer(static_cast<int64_t>(r.events));
    w.key("wall_ms").number(r.wall_ms, 2);
    w.key("events_per_s").number(r.wall_ms > 0 ? r.events / (r.wall_ms / 1000.0) : 0.0, 0);
    w.key("baseline").begin_object();
    w.key("goodput").number(r.baseline_goodput, 2);
    w.key("offered").number(r.baseline_offered, 2);
    w.key("success_ratio").number(r.baseline_offered > 0 ? r.baseline_goodput / r.baseline_offered : 0.0, 4);
    w.end_object();
    if (r.trigger_start >= 0) {
      w.key("trigger").begin_object();
      w.key("start_s").number(r.trigger_start, 3);
      w.key("end_s").number(r.trigger_end, 3);
      w.end_object();
    } else {
      w.key("trigger").null();
    }
    if (r.recovery_s >= 0) w.key("recovery_s").number(r.recovery_s, 2);
    else w.key("recovery_s").null();
    w.key("min_goodput_ratio").number(r.min_goodput_ratio, 4);
    w.key("peak_amplification").number(r.peak_amp, 3);
    w.key("wasted_fraction").number(r.wasted_fraction, 4);
    w.key("lost_requests").number(r.lost_requests, 0);
    w.key("latency_ms").begin_object();
    w.key("p99_pre").number(r.p99_pre, 2);
    w.key("p99_during").number(r.p99_during, 2);
    w.key("p99_post").number(r.p99_post, 2);
    w.key("p50").number(ctot.latency.count() ? ctot.latency.percentile(0.5) : std::nan(""), 2);
    w.key("p99").number(ctot.latency.count() ? ctot.latency.percentile(0.99) : std::nan(""), 2);
    w.end_object();
    w.key("totals").begin_object();
    w.key("offered").integer(static_cast<int64_t>(ctot.offered));
    w.key("ok").integer(static_cast<int64_t>(ctot.ok));
    w.key("good").integer(static_cast<int64_t>(ctot.good));
    w.key("err").integer(static_cast<int64_t>(ctot.err));
    w.key("retries").integer(static_cast<int64_t>(ctot.retries));
    w.end_object();
    w.key("services").begin_array();
    for (size_t s = 0; s < svc.size(); ++s) {
      const ServiceTotals& T = stot[s];
      w.begin_object();
      w.key("name").string(spec.services[s].name);
      w.key("arrivals").integer(static_cast<int64_t>(T.arrivals));
      w.key("ok").integer(static_cast<int64_t>(T.ok));
      w.key("err").integer(static_cast<int64_t>(T.err));
      w.key("shed").integer(static_cast<int64_t>(T.shed));
      w.key("dropped").integer(static_cast<int64_t>(T.dropped));
      w.key("wasted").integer(static_cast<int64_t>(T.wasted));
      w.key("timeouts").integer(static_cast<int64_t>(T.timeouts));
      w.key("retries").integer(static_cast<int64_t>(T.retries));
      w.key("cb_rejects").integer(static_cast<int64_t>(T.cb_rejects));
      w.key("budget_denied").integer(static_cast<int64_t>(T.budget_denied));
      w.key("p50").number(T.latency.percentile(0.5), 2);
      w.key("p99").number(T.latency.percentile(0.99), 2);
      w.end_object();
    }
    w.end_array();
    if (include_series) {
      w.key("series").begin_object();
      auto col = [&](const char* name, auto get, int prec) {
        w.key(name).begin_array();
        for (const SeriesPoint& p : series) w.number(get(p), prec);
        w.end_array();
      };
      col("t", [](const SeriesPoint& p) { return p.t; }, 3);
      col("offered", [](const SeriesPoint& p) { return p.offered; }, 1);
      col("goodput", [](const SeriesPoint& p) { return p.good; }, 1);
      col("ok", [](const SeriesPoint& p) { return p.ok; }, 1);
      col("errors", [](const SeriesPoint& p) { return p.err; }, 1);
      col("p50", [](const SeriesPoint& p) { return p.p50; }, 2);
      col("p99", [](const SeriesPoint& p) { return p.p99; }, 2);
      col("amp", [](const SeriesPoint& p) { return p.amp; }, 3);
      col("wasted", [](const SeriesPoint& p) { return p.wasted; }, 4);
      w.end_object();
    }
    w.end_object();
    return w.take();
  }

  std::string apply(const json::Value& cmd) {
    if (!cmd.is_object()) return "command must be a JSON object";
    const json::Value* opv = cmd.find("op");
    if (!opv || !opv->is_string()) return "missing 'op'";
    const std::string& op = opv->as_string();
    auto service_arg = [&](const char* key, int* out) -> std::string {
      const json::Value* v = cmd.find(key);
      if (!v || !v->is_string()) return std::string("missing '") + key + "'";
      *out = spec.find_service(v->as_string());
      if (*out < 0) return "unknown service '" + v->as_string() + "'";
      return "";
    };
    try {
      if (op == "fault") {
        const json::Value* fv = cmd.find("fault");
        if (!fv) return "missing 'fault'";
        FaultRt fr;
        fr.f = parse_fault(*fv, spec, "fault");
        fr.f.start_s = now;
        fr.scheduled = false;
        faults.push_back(fr);
        on_fault_start(static_cast<uint32_t>(faults.size() - 1));
        return "";
      }
      if (op == "clear_faults") {
        for (uint32_t i = 0; i < faults.size(); ++i) {
          if (faults[i].active) on_fault_end(i);
        }
        return "";
      }
      if (op == "load") {
        const json::Value* m = cmd.find("multiplier");
        if (!m || !m->is_number() || m->as_number() < 0 || m->as_number() > 100) return "'multiplier' must be a number in [0, 100]";
        spec.load_multiplier = m->as_number();
        reschedule_arrival();
        return "";
      }
      if (op == "scale") {
        int s;
        if (auto e = service_arg("service", &s); !e.empty()) return e;
        const json::Value* n = cmd.find("instances");
        if (!n || !n->is_number() || n->as_number() < 1 || n->as_number() > 1000) return "'instances' must be in [1, 1000]";
        const int want = static_cast<int>(n->as_number());
        ServiceRt& S = svc[static_cast<size_t>(s)];
        int have = static_cast<int>(S.alive.size());
        while (have < want) {
          add_instance(s);
          ++have;
        }
        if (have > want) {
          int remove = have - want;
          std::vector<int> cand = S.alive;
          for (auto it = cand.rbegin(); it != cand.rend() && remove > 0; ++it, --remove) {
            S.inst[static_cast<size_t>(*it)].draining = true;
          }
          rebuild_alive(s);
          for (int i : cand) dispatch(s, i);
        }
        return "";
      }
      if (op == "policy") {
        int s = -1;
        const json::Value* from = cmd.find("service");
        if (!from || !from->is_string()) return "missing 'service'";
        int target;
        if (auto e = service_arg("target", &target); !e.empty()) return e;
        RetryPolicy* rp = nullptr;
        BudgetPolicy* bp = nullptr;
        BreakerPolicy* kp = nullptr;
        double* timeout = nullptr;
        EdgeRt* E = nullptr;
        if (from->as_string() == "client") {
          if (target != spec.entry) return "the client only calls the entry service";
          rp = &spec.client.retry;
          bp = &spec.client.budget;
          kp = &spec.client.breaker;
          timeout = &spec.client.timeout_s;
          E = &client_edge;
        } else {
          s = spec.find_service(from->as_string());
          if (s < 0) return "unknown service '" + from->as_string() + "'";
          auto& cs = spec.services[static_cast<size_t>(s)].calls;
          size_t k = 0;
          while (k < cs.size() && cs[k].target != target) ++k;
          if (k == cs.size()) return "'" + from->as_string() + "' does not call that target";
          rp = &cs[k].retry;
          bp = &cs[k].budget;
          kp = &cs[k].breaker;
          timeout = &cs[k].timeout_s;
          E = &svc[static_cast<size_t>(s)].edges[k];
        }
        if (const json::Value* v = cmd.find("retry")) *rp = parse_retry(*v, "retry");
        if (const json::Value* v = cmd.find("budget")) {
          *bp = parse_budget(*v, "budget");
          E->tokens = 0;
          E->refill_t = now;
        }
        if (const json::Value* v = cmd.find("breaker")) {
          *kp = parse_breaker(*v, "breaker");
          E->breaker = Breaker{};
        }
        if (const json::Value* v = cmd.find("timeout_ms")) {
          if (!v->is_number() || v->as_number() <= 0 || v->as_number() > 3600000) return "'timeout_ms' must be > 0";
          *timeout = v->as_number() / 1000.0;
        }
        return "";
      }
      if (op == "resilience") {
        if (const json::Value* v = cmd.find("deadline_propagation"); v && v->is_bool()) spec.deadline_propagation = v->as_bool();
        if (const json::Value* v = cmd.find("cancel_on_timeout"); v && v->is_bool()) spec.cancel_on_timeout = v->as_bool();
        return "";
      }
      if (op == "queue") {
        int s;
        if (auto e = service_arg("service", &s); !e.empty()) return e;
        ServiceSpec& ss = spec.services[static_cast<size_t>(s)];
        if (const json::Value* v = cmd.find("discipline"); v && v->is_string()) {
          if (!parse_queue_discipline(v->as_string(), &ss.discipline)) return "unknown discipline";
        }
        if (const json::Value* v = cmd.find("capacity"); v && v->is_number()) ss.queue_capacity = static_cast<int>(std::clamp(v->as_number(), 0.0, 1e7));
        if (const json::Value* v = cmd.find("timeout_ms"); v && v->is_number()) ss.queue_timeout_s = std::clamp(v->as_number(), 0.0, 3.6e6) / 1000.0;
        return "";
      }
      if (op == "lb") {
        int s;
        if (auto e = service_arg("service", &s); !e.empty()) return e;
        const json::Value* v = cmd.find("policy");
        if (!v || !v->is_string() || !parse_lb(v->as_string(), &spec.services[static_cast<size_t>(s)].lb)) return "unknown lb policy";
        return "";
      }
    } catch (const SpecError& e) {
      return e.what();
    }
    return "unknown op '" + op + "'";
  }
};

Simulator::Simulator(const Spec& spec, bool emit_frames) : impl_(std::make_unique<Impl>(spec, emit_frames)) {}
Simulator::~Simulator() = default;

bool Simulator::advance_to(double t_s) { return impl_->advance_to(t_s); }
void Simulator::run() { impl_->advance_to(kInf); }
bool Simulator::finished() const { return impl_->finished; }
double Simulator::now() const { return impl_->now; }
uint64_t Simulator::events() const { return impl_->events; }

std::string Simulator::take_frames() {
  std::string out;
  if (impl_->frames_open) {
    impl_->frames.push_back(']');
    out.swap(impl_->frames);
    impl_->frames_open = false;
  } else {
    out = "[]";
  }
  impl_->frames.clear();
  return out;
}

RunSummary Simulator::summary() const { return impl_->summarize(); }
std::string Simulator::summary_json(bool include_series) const { return impl_->summary_json(include_series); }

std::string Simulator::command(std::string_view json_text) {
  json::Value v;
  try {
    v = json::parse(json_text);
  } catch (const json::ParseError& e) {
    return e.what();
  }
  return impl_->apply(v);
}

const ClientTotals& Simulator::client_totals() const { return impl_->ctot; }
const ServiceTotals& Simulator::service_totals(int service) const { return impl_->stot[static_cast<size_t>(service)]; }
const std::vector<SeriesPoint>& Simulator::series() const { return impl_->series; }
const Spec& Simulator::spec() const { return impl_->spec; }

}
