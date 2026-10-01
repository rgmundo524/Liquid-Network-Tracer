/* Optional Linux-only instrumentation for benchmark_address_counts.py.
 *
 * Build with cc -shared -fPIC -std=c11 -O2 -o helper.so this_file.c -ldl -pthread.
 * The Python benchmark compiles and loads this helper only when --measure-sync
 * or a --*-sync-delay-ms option is selected. No application code loads it.
 *
 * Every fsync/fdatasync still reaches the real implementation. We only count,
 * time, and optionally delay syncs of files in liquid-count-benchmark-* scratch
 * directories. In particular, quota directory syncs count too: this captures
 * the different durable-I/O costs of SQLite DELETE journals and persistent WAL.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

enum { EVIDENCE, QUOTA, OTHER, KINDS };
static const char *names[KINDS] = {"evidence", "quota", "other"};
static _Atomic uint64_t calls[KINDS][2], elapsed_ns[KINDS], injected_ns[KINDS], errors[KINDS];
static uint64_t delay_ns[KINDS];
static int (*actual_fsync)(int), (*actual_fdatasync)(int);
static pthread_once_t initialized = PTHREAD_ONCE_INIT;

static uint64_t monotonic_ns(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (uint64_t)value.tv_sec * 1000000000ULL + value.tv_nsec;
}

static void initialize(void) {
    actual_fsync = dlsym(RTLD_NEXT, "fsync");
    actual_fdatasync = dlsym(RTLD_NEXT, "fdatasync");
    const char *variables[2] = {"LIQUID_BENCH_EVIDENCE_SYNC_DELAY_MS", "LIQUID_BENCH_QUOTA_SYNC_DELAY_MS"};
    for (int kind = EVIDENCE; kind <= QUOTA; kind++) {
        const char *raw = getenv(variables[kind]);
        if (raw) {
            double value = strtod(raw, NULL);
            if (isfinite(value) && value >= 0. && value <= 1000.)
                delay_ns[kind] = (uint64_t)(value * 1000000.);
        }
    }
}

static int descriptor_kind(int descriptor) {
    char link[64], path[PATH_MAX + 1];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", descriptor);
    ssize_t length = readlink(link, path, PATH_MAX);
    if (length <= 0)
        return -1;
    path[length] = '\0';
    const char *scratch = strstr(path, "/liquid-count-benchmark-");
    if (!scratch)
        return -1;
    const char *quota = strstr(scratch, "/quota");
    if (quota && (quota[6] == '/' || quota[6] == '\0'))
        return QUOTA;
    const char *file = strrchr(path, '/');
    if (file && strncmp(file + 1, "evidence.sqlite", 15) == 0)
        return EVIDENCE;
    return OTHER;
}

static int instrument_sync(int descriptor, int data_only) {
    pthread_once(&initialized, initialize);
    int (*operation)(int) = data_only ? actual_fdatasync : actual_fsync;
    if (!operation) {
        errno = ENOSYS;
        return -1;
    }
    int previous_errno = errno;
    int kind = descriptor_kind(descriptor);
    errno = previous_errno;
    if (kind < 0)
        return operation(descriptor);
    uint64_t begun = monotonic_ns(), injected = 0;
    if (delay_ns[kind]) {
        struct timespec wait = {(time_t)(delay_ns[kind] / 1000000000ULL),
                                (long)(delay_ns[kind] % 1000000000ULL)};
        while (nanosleep(&wait, &wait) != 0 && errno == EINTR) {}
        injected = monotonic_ns() - begun;
    }
    errno = previous_errno;
    int result = operation(descriptor);
    int operation_errno = errno;
    atomic_fetch_add(&calls[kind][data_only], 1);
    atomic_fetch_add(&elapsed_ns[kind], monotonic_ns() - begun);
    atomic_fetch_add(&injected_ns[kind], injected);
    if (result != 0)
        atomic_fetch_add(&errors[kind], 1);
    errno = operation_errno;
    return result;
}

int fsync(int descriptor) { return instrument_sync(descriptor, 0); }
int fdatasync(int descriptor) { return instrument_sync(descriptor, 1); }

__attribute__((destructor)) static void report(void) {
    const char *path = getenv("LIQUID_BENCH_SYNC_REPORT");
    if (!path || !*path)
        return;
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (descriptor < 0)
        return;
    dprintf(descriptor, "{\"schema_version\":1,\"real_syncs_preserved\":true,\"by_kind\":{");
    for (int kind = 0; kind < KINDS; kind++) {
        dprintf(descriptor,
                "%s\"%s\":{\"fsync\":%llu,\"fdatasync\":%llu,\"seconds\":%.9f,"
                "\"injected_wait_seconds\":%.9f,\"errors\":%llu}",
                kind ? "," : "", names[kind],
                (unsigned long long)atomic_load(&calls[kind][0]),
                (unsigned long long)atomic_load(&calls[kind][1]),
                atomic_load(&elapsed_ns[kind]) / 1e9,
                atomic_load(&injected_ns[kind]) / 1e9,
                (unsigned long long)atomic_load(&errors[kind]));
    }
    dprintf(descriptor, "}}\n");
    close(descriptor);
}
