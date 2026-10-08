#include <cstdlib>
#include <cstring>
#include <new>
#include <string>

#include "kessler/kessler.h"
#include "sim.hpp"
#include "spec.hpp"

struct ks_sim {
  kessler::Simulator sim;
  ks_sim(const kessler::Spec& s, bool frames) : sim(s, frames) {}
};

namespace {

char* dup(const std::string& s, size_t* out_len = nullptr) {
  char* p = static_cast<char*>(std::malloc(s.size() + 1));
  if (!p) return nullptr;
  std::memcpy(p, s.data(), s.size());
  p[s.size()] = '\0';
  if (out_len) *out_len = s.size();
  return p;
}

void set_err(char** err, const std::string& msg) {
  if (err) *err = dup(msg);
}

}

extern "C" {

KS_API const char* ks_version(void) { return kessler::kEngineVersion; }

KS_API ks_sim* ks_sim_create(const char* spec_json, size_t len, int emit_frames, char** err) {
  if (err) *err = nullptr;
  if (!spec_json) {
    set_err(err, "spec_json is NULL");
    return nullptr;
  }
  try {
    const kessler::Spec spec = kessler::parse_spec_text(std::string_view(spec_json, len));
    return new ks_sim(spec, emit_frames != 0);
  } catch (const std::exception& e) {
    set_err(err, e.what());
  } catch (...) {
    set_err(err, "unknown error");
  }
  return nullptr;
}

KS_API void ks_sim_destroy(ks_sim* sim) { delete sim; }

KS_API int ks_sim_advance(ks_sim* sim, double until_s) {
  if (!sim) return 0;
  try {
    return sim->sim.advance_to(until_s) ? 1 : 0;
  } catch (...) {
    return 0;
  }
}

KS_API void ks_sim_run(ks_sim* sim) {
  if (!sim) return;
  try {
    sim->sim.run();
  } catch (...) {
  }
}

KS_API double ks_sim_now(const ks_sim* sim) { return sim ? sim->sim.now() : 0.0; }
KS_API int64_t ks_sim_events(const ks_sim* sim) { return sim ? static_cast<int64_t>(sim->sim.events()) : 0; }
KS_API int ks_sim_finished(const ks_sim* sim) { return sim && sim->sim.finished() ? 1 : 0; }

KS_API char* ks_sim_take_frames(ks_sim* sim, size_t* out_len) {
  if (!sim) return nullptr;
  try {
    return dup(sim->sim.take_frames(), out_len);
  } catch (...) {
    return nullptr;
  }
}

KS_API char* ks_sim_summary(const ks_sim* sim, int include_series, size_t* out_len) {
  if (!sim) return nullptr;
  try {
    return dup(sim->sim.summary_json(include_series != 0), out_len);
  } catch (...) {
    return nullptr;
  }
}

KS_API int ks_sim_command(ks_sim* sim, const char* cmd_json, size_t len, char** err) {
  if (err) *err = nullptr;
  if (!sim || !cmd_json) {
    set_err(err, "invalid arguments");
    return -1;
  }
  try {
    const std::string e = sim->sim.command(std::string_view(cmd_json, len));
    if (e.empty()) return 0;
    set_err(err, e);
  } catch (const std::exception& e) {
    set_err(err, e.what());
  } catch (...) {
    set_err(err, "unknown error");
  }
  return -1;
}

KS_API char* ks_analyze(const char* spec_json, size_t len, char** err) {
  if (err) *err = nullptr;
  try {
    const kessler::Spec spec = kessler::parse_spec_text(std::string_view(spec_json, len));
    return dup(kessler::static_analysis_json(spec));
  } catch (const std::exception& e) {
    set_err(err, e.what());
  } catch (...) {
    set_err(err, "unknown error");
  }
  return nullptr;
}

KS_API char* ks_run(const char* spec_json, size_t len, int include_series, char** err) {
  if (err) *err = nullptr;
  try {
    const kessler::Spec spec = kessler::parse_spec_text(std::string_view(spec_json, len));
    kessler::Simulator sim(spec, false);
    sim.run();
    return dup(sim.summary_json(include_series != 0));
  } catch (const std::exception& e) {
    set_err(err, e.what());
  } catch (...) {
    set_err(err, "unknown error");
  }
  return nullptr;
}

KS_API void ks_free(void* p) { std::free(p); }

}
