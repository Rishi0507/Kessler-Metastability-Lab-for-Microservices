#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

#include "json.hpp"
#include "sim.hpp"

namespace kessler {

namespace {

std::vector<int> topo_from(const Spec& spec, int root) {
  const size_t n = spec.services.size();
  std::vector<int> order;
  std::vector<char> seen(n, 0);
  std::vector<std::pair<int, size_t>> stack{{root, 0}};
  seen[static_cast<size_t>(root)] = 1;
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
  return order;
}

double effective_probability(const ServiceSpec& ss, size_t k) {
  double p = ss.calls[k].probability;
  if (ss.cache.enabled && static_cast<int>(k) == ss.cache.miss_call) p *= 1.0 - ss.cache.hit_ratio;
  return p;
}

double worst_case_s(const Spec& spec, int s) {
  const ServiceSpec& ss = spec.services[static_cast<size_t>(s)];
  double t = ss.service_time.p99();
  for (const CallSpec& c : ss.calls) {
    const int a = c.retry.max_attempts;
    t += a * c.timeout_s + (a - 1) * c.retry.cap_s;
  }
  return t;
}

}

std::string static_analysis_json(const Spec& spec) {
  const size_t n = spec.services.size();
  const std::vector<int> order = topo_from(spec, spec.entry);
  const double rtt = 2.0 * spec.net_latency_s;

  std::vector<double> visits(n, 0.0);
  visits[static_cast<size_t>(spec.entry)] = 1.0;
  for (int u : order) {
    const ServiceSpec& ss = spec.services[static_cast<size_t>(u)];
    for (size_t k = 0; k < ss.calls.size(); ++k) {
      visits[static_cast<size_t>(ss.calls[k].target)] += visits[static_cast<size_t>(u)] * effective_probability(ss, k);
    }
  }

  std::vector<double> hold(n, 0.0);
  for (auto it = order.rbegin(); it != order.rend(); ++it) {
    const int u = *it;
    const ServiceSpec& ss = spec.services[static_cast<size_t>(u)];
    double h = ss.service_time.mean();
    for (size_t k = 0; k < ss.calls.size(); ++k) {
      h += effective_probability(ss, k) * (rtt + hold[static_cast<size_t>(ss.calls[k].target)]);
    }
    hold[static_cast<size_t>(u)] = h;
  }

  const double offered = spec.rps * spec.load_multiplier;
  double max_rps = std::numeric_limits<double>::infinity();
  int bottleneck = -1;
  std::vector<double> capacity(n, 0.0);
  for (size_t s = 0; s < n; ++s) {
    if (hold[s] <= 0) continue;
    capacity[s] = spec.services[s].instances * spec.services[s].concurrency / hold[s];
    if (visits[s] > 0) {
      const double r = capacity[s] / visits[s];
      if (r < max_rps) {
        max_rps = r;
        bottleneck = static_cast<int>(s);
      }
    }
  }

  std::vector<double> amp(n, 0.0);
  std::vector<int> amp_parent(n, -1);
  amp[static_cast<size_t>(spec.entry)] = spec.client.retry.max_attempts;
  for (int u : order) {
    const ServiceSpec& ss = spec.services[static_cast<size_t>(u)];
    for (const CallSpec& c : ss.calls) {
      const double a = amp[static_cast<size_t>(u)] * c.retry.max_attempts;
      if (a > amp[static_cast<size_t>(c.target)]) {
        amp[static_cast<size_t>(c.target)] = a;
        amp_parent[static_cast<size_t>(c.target)] = u;
      }
    }
  }
  int worst = spec.entry;
  for (size_t s = 0; s < n; ++s) {
    if (amp[s] > amp[static_cast<size_t>(worst)]) worst = static_cast<int>(s);
  }
  std::vector<int> path;
  for (int v = worst; v >= 0; v = amp_parent[static_cast<size_t>(v)]) path.push_back(v);
  std::reverse(path.begin(), path.end());

  json::Writer w;
  w.begin_object();
  w.key("engine").string(kEngineVersion);
  w.key("offered_rps").number(offered, 2);
  w.key("max_rps").number(std::isfinite(max_rps) ? max_rps : 0.0, 2);
  w.key("max_load_multiplier").number(std::isfinite(max_rps) && offered > 0 ? max_rps / spec.rps : 0.0, 4);
  w.key("headroom").number(std::isfinite(max_rps) && offered > 0 ? max_rps / offered : 0.0, 4);
  w.key("bottleneck").string(bottleneck >= 0 ? spec.services[static_cast<size_t>(bottleneck)].name : "");
  w.key("critical_path_ms").number((hold[static_cast<size_t>(spec.entry)] + rtt) * 1000.0, 3);
  w.key("worst_amplification").number(amp[static_cast<size_t>(worst)], 2);
  w.key("worst_amplification_path").begin_array();
  w.string("client");
  for (int v : path) w.string(spec.services[static_cast<size_t>(v)].name);
  w.end_array();

  w.key("services").begin_array();
  for (size_t s = 0; s < n; ++s) {
    const ServiceSpec& ss = spec.services[s];
    const double lambda = offered * visits[s];
    const double per_instance_rate = hold[s] > 0 ? ss.concurrency / hold[s] : 0.0;
    w.begin_object();
    w.key("name").string(ss.name);
    w.key("reachable").boolean(visits[s] > 0 || static_cast<int>(s) == spec.entry);
    w.key("visits").number(visits[s], 4);
    w.key("service_time_mean_ms").number(ss.service_time.mean() * 1000.0, 3);
    w.key("service_time_p99_ms").number(ss.service_time.p99() * 1000.0, 3);
    w.key("hold_ms").number(hold[s] * 1000.0, 3);
    w.key("capacity_rps").number(capacity[s], 2);
    w.key("arrival_rps").number(lambda, 2);
    w.key("utilization").number(capacity[s] > 0 ? lambda / capacity[s] : 0.0, 4);
    w.key("instances").integer(ss.instances);
    w.key("concurrency").integer(ss.concurrency);
    w.key("queue_capacity").integer(ss.queue_capacity);
    w.key("queue_discipline").string(to_string(ss.discipline));
    w.key("max_queue_wait_ms").number(per_instance_rate > 0 ? ss.queue_capacity / per_instance_rate * 1000.0 : 0.0, 2);
    w.key("worst_case_ms").number(worst_case_s(spec, static_cast<int>(s)) * 1000.0, 2);
    w.key("amplification").number(amp[s], 2);
    w.key("cache").boolean(ss.cache.enabled);
    w.key("autoscale").boolean(ss.autoscale.enabled);
    w.end_object();
  }
  w.end_array();

  auto edge = [&](const char* from, int to, double timeout_s, const RetryPolicy& r, const BudgetPolicy& b,
                  const BreakerPolicy& k, double probability) {
    w.begin_object();
    w.key("from").string(from);
    w.key("to").string(spec.services[static_cast<size_t>(to)].name);
    w.key("probability").number(probability, 4);
    w.key("timeout_ms").number(timeout_s * 1000.0, 3);
    w.key("max_attempts").integer(r.max_attempts);
    w.key("backoff_base_ms").number(r.base_s * 1000.0, 3);
    w.key("backoff_cap_ms").number(r.cap_s * 1000.0, 3);
    w.key("jitter").string(to_string(r.jitter));
    w.key("retry_on_timeout").boolean(r.on_timeout);
    w.key("budget").boolean(b.enabled);
    w.key("budget_ratio").number(b.enabled ? b.ratio : 0.0, 4);
    w.key("breaker").boolean(k.enabled);
    w.key("callee_worst_case_ms").number(worst_case_s(spec, to) * 1000.0, 2);
    w.key("callee_hold_ms").number(hold[static_cast<size_t>(to)] * 1000.0, 3);
    w.end_object();
  };
  w.key("edges").begin_array();
  edge("client", spec.entry, spec.client.timeout_s, spec.client.retry, spec.client.budget, spec.client.breaker, 1.0);
  for (size_t s = 0; s < n; ++s) {
    const ServiceSpec& ss = spec.services[s];
    for (size_t k = 0; k < ss.calls.size(); ++k) {
      const CallSpec& c = ss.calls[k];
      edge(ss.name.c_str(), c.target, c.timeout_s, c.retry, c.budget, c.breaker, effective_probability(ss, k));
    }
  }
  w.end_array();
  w.key("resilience").begin_object();
  w.key("deadline_propagation").boolean(spec.deadline_propagation);
  w.key("cancel_on_timeout").boolean(spec.cancel_on_timeout);
  w.end_object();
  w.end_object();
  return w.take();
}

}
