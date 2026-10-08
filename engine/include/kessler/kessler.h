#ifndef KESSLER_ENGINE_H
#define KESSLER_ENGINE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(KESSLER_BUILD_DLL)
#    define KS_API __declspec(dllexport)
#  elif defined(KESSLER_STATIC)
#    define KS_API
#  else
#    define KS_API __declspec(dllimport)
#  endif
#else
#  define KS_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ks_sim ks_sim;

KS_API const char* ks_version(void);

KS_API ks_sim* ks_sim_create(const char* spec_json, size_t len, int emit_frames, char** err);
KS_API void ks_sim_destroy(ks_sim* sim);

KS_API int ks_sim_advance(ks_sim* sim, double until_s);
KS_API void ks_sim_run(ks_sim* sim);

KS_API double ks_sim_now(const ks_sim* sim);
KS_API int64_t ks_sim_events(const ks_sim* sim);
KS_API int ks_sim_finished(const ks_sim* sim);

KS_API char* ks_sim_take_frames(ks_sim* sim, size_t* out_len);
KS_API char* ks_sim_summary(const ks_sim* sim, int include_series, size_t* out_len);
KS_API int ks_sim_command(ks_sim* sim, const char* cmd_json, size_t len, char** err);

KS_API char* ks_analyze(const char* spec_json, size_t len, char** err);
KS_API char* ks_run(const char* spec_json, size_t len, int include_series, char** err);

KS_API void ks_free(void* p);

#ifdef __cplusplus
}
#endif

#endif
