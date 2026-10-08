#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "../src/json.hpp"
#include "../src/parallel.hpp"
#include "../src/sim.hpp"
#include "../src/spec.hpp"

using namespace kessler;

namespace {

constexpr const char* kBenchSpec = R"({
  "seed": 7, "horizon_s": 120, "warmup_s": 20,
  "traffic": {"rps": 1500, "entry": "gateway",
              "client": {"timeout_ms": 2000, "retry": {"max_attempts": 2, "base_ms": 100, "jitter": "full"}}},
  "services": [
    {"name": "gateway", "instances": 4, "concurrency": 64, "lb": "p2c",
     "service_time": {"dist": "lognormal", "p50_ms": 1, "p99_ms": 4},
     "calls": [{"to": "api", "timeout_ms": 800, "retry": {"max_attempts": 2, "base_ms": 20, "jitter": "full"}}]},
    {"name": "api", "instances": 6, "concurrency": 32, "lb": "least_outstanding",
     "service_time": {"dist": "lognormal", "p50_ms": 3, "p99_ms": 12},
     "calls": [{"to": "cache", "timeout_ms": 50},
               {"to": "search", "probability": 0.3, "timeout_ms": 300}]},
    {"name": "cache", "instances": 3, "concurrency": 64,
     "service_time": {"dist": "lognormal", "p50_ms": 0.3, "p99_ms": 1.5},
     "cache": {"hit_ratio": 0.9, "key_space": 50000, "miss_target": "db"},
     "calls": [{"to": "db", "timeout_ms": 250, "retry": {"max_attempts": 2, "base_ms": 10}}]},
    {"name": "db", "instances": 2, "concurrency": 32,
     "service_time": {"dist": "lognormal", "p50_ms": 4, "p99_ms": 25},
     "contention": {"knee": 0.7, "factor": 2}},
    {"name": "search", "instances": 4, "concurrency": 16,
     "service_time": {"dist": "lognormal", "p50_ms": 8, "p99_ms": 40}}
  ],
  "faults": [{"kind": "latency", "target": "db", "magnitude": 3, "start_s": 60, "duration_s": 15}]
})";

std::string read_file(const char* path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) {
    std::fprintf(stderr, "error: cannot open %s\n", path);
    std::exit(2);
  }
  std::stringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

const char* flag_value(int argc, char** argv, const char* name, const char* def) {
  for (int i = 0; i + 1 < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return argv[i + 1];
  }
  return def;
}

bool has_flag(int argc, char** argv, const char* name) {
  for (int i = 0; i < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return true;
  }
  return false;
}

std::vector<double> parse_range(const char* s) {
  double lo = 0, hi = 0;
  int n = 0;
  if (std::sscanf(s, "%lf:%lf:%d", &lo, &hi, &n) != 3 || n < 1) {
    std::fprintf(stderr, "error: range must be lo:hi:n (got %s)\n", s);
    std::exit(2);
  }
  std::vector<double> v;
  for (int i = 0; i < n; ++i) v.push_back(n == 1 ? lo : lo + (hi - lo) * i / (n - 1));
  return v;
}

Spec load_spec(const char* path) {
  try {
    return parse_spec_text(read_file(path));
  } catch (const std::exception& e) {
    std::fprintf(stderr, "invalid spec: %s\n", e.what());
    std::exit(2);
  }
}

int cmd_run(int argc, char** argv) {
  if (argc < 3) return 2;
  const Spec spec = load_spec(argv[2]);
  const bool frames = has_flag(argc, argv, "--frames");
  Simulator sim(spec, frames);
  sim.run();
  if (frames) std::printf("%s\n", sim.take_frames().c_str());
  std::printf("%s\n", sim.summary_json(has_flag(argc, argv, "--series")).c_str());
  return 0;
}

int cmd_analyze(int argc, char** argv) {
  if (argc < 3) return 2;
  std::printf("%s\n", static_analysis_json(load_spec(argv[2])).c_str());
  return 0;
}

struct Cell {
  double load, mag;
  uint64_t seed;
  RunSummary r;
};

int cmd_sweep(int argc, char** argv) {
  if (argc < 3) return 2;
  const Spec base = load_spec(argv[2]);
  const int fault = std::atoi(flag_value(argc, argv, "--fault", "0"));
  if (fault < 0 || fault >= static_cast<int>(base.faults.size())) {
    std::fprintf(stderr, "error: spec has no fault #%d\n", fault);
    return 2;
  }
  const auto loads = parse_range(flag_value(argc, argv, "--loads", "0.5:1.0:6"));
  const auto mags = parse_range(flag_value(argc, argv, "--mags", "1:8:6"));
  const int seeds = std::atoi(flag_value(argc, argv, "--seeds", "2"));
  const int threads = std::atoi(flag_value(argc, argv, "--threads", "0"));

  std::vector<Cell> cells;
  for (double m : mags)
    for (double l : loads)
      for (int k = 0; k < seeds; ++k) cells.push_back(Cell{l, m, base.seed + static_cast<uint64_t>(k), {}});

  std::atomic<uint64_t> total_events{0};
  const auto t0 = std::chrono::steady_clock::now();
  parallel_for(cells.size(), threads > 0 ? threads : default_threads(), [&](size_t i) {
    Spec s = base;
    s.load_multiplier = cells[i].load;
    s.faults[static_cast<size_t>(fault)].magnitude = cells[i].mag;
    s.seed = cells[i].seed;
    Simulator sim(s);
    sim.run();
    cells[i].r = sim.summary();
    total_events.fetch_add(sim.events(), std::memory_order_relaxed);
  });
  const double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

  for (const Cell& c : cells) {
    std::printf("{\"load\":%.4g,\"magnitude\":%.4g,\"seed\":%llu,\"verdict\":\"%s\",\"recovery_s\":%s,\"peak_amp\":%.3f,\"events\":%llu}\n",
                c.load, c.mag, static_cast<unsigned long long>(c.seed), to_string(c.r.verdict),
                c.r.recovery_s >= 0 ? std::to_string(c.r.recovery_s).c_str() : "null", c.r.peak_amp,
                static_cast<unsigned long long>(c.r.events));
  }
  std::fprintf(stderr, "sweep: %zu sims in %.2fs (%.1f sims/s, %.2fM events/s)\n", cells.size(), wall,
               cells.size() / wall, total_events.load() / wall / 1e6);
  return 0;
}

int cmd_bench(int argc, char** argv) {
  const Spec spec = parse_spec_text(kBenchSpec);
  const int threads = std::atoi(flag_value(argc, argv, "--threads", "0")) > 0
                          ? std::atoi(flag_value(argc, argv, "--threads", "0"))
                          : default_threads();
  const int sims = std::atoi(flag_value(argc, argv, "--sims", "8"));

  uint64_t ev1 = 0;
  const auto a = std::chrono::steady_clock::now();
  for (int i = 0; i < 2; ++i) {
    Spec s = spec;
    s.seed += static_cast<uint64_t>(i);
    Simulator sim(s);
    sim.run();
    ev1 += sim.events();
  }
  const double w1 = std::chrono::duration<double>(std::chrono::steady_clock::now() - a).count();

  std::atomic<uint64_t> evn{0};
  const auto b = std::chrono::steady_clock::now();
  parallel_for(static_cast<size_t>(sims), threads, [&](size_t i) {
    Spec s = spec;
    s.seed += 100 + i;
    Simulator sim(s);
    sim.run();
    evn.fetch_add(sim.events(), std::memory_order_relaxed);
  });
  const double wn = std::chrono::duration<double>(std::chrono::steady_clock::now() - b).count();

  const double single = ev1 / w1;
  const double multi = evn.load() / wn;
  std::printf(
      "{\"engine\":\"%s\",\"single_thread_events_per_s\":%.0f,\"threads\":%d,\"parallel_events_per_s\":%.0f,"
      "\"speedup\":%.2f,\"sims_per_s\":%.2f,\"events_per_sim\":%.0f}\n",
      kEngineVersion, single, threads, multi, multi / single, sims / wn, static_cast<double>(ev1) / 2.0);
  return 0;
}

void usage() {
  std::fprintf(stderr,
               "kessler-sim %s\n"
               "usage:\n"
               "  kessler-sim run <spec.json> [--series] [--frames]\n"
               "  kessler-sim analyze <spec.json>\n"
               "  kessler-sim sweep <spec.json> [--fault N] [--loads lo:hi:n] [--mags lo:hi:n] [--seeds k] [--threads t]\n"
               "  kessler-sim bench [--threads t] [--sims n]\n",
               kEngineVersion);
}

}

int main(int argc, char** argv) {
  if (argc < 2) {
    usage();
    return 2;
  }
  const std::string cmd = argv[1];
  int rc = 2;
  if (cmd == "run") rc = cmd_run(argc, argv);
  else if (cmd == "analyze") rc = cmd_analyze(argc, argv);
  else if (cmd == "sweep") rc = cmd_sweep(argc, argv);
  else if (cmd == "bench") rc = cmd_bench(argc, argv);
  if (rc == 2) usage();
  return rc;
}
