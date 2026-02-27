#define _GNU_SOURCE

#include <dlfcn.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef O_TMPFILE
#define O_TMPFILE 0
#endif

typedef struct PrefixList {
    char **items;
    size_t count;
} PrefixList;

static PrefixList g_deny = {0};
static PrefixList g_allow = {0};
static pthread_once_t g_once = PTHREAD_ONCE_INIT;

static int (*real_open_fn)(const char *pathname, int flags, ...) = NULL;
static int (*real_open64_fn)(const char *pathname, int flags, ...) = NULL;
static int (*real_openat_fn)(int dirfd, const char *pathname, int flags, ...) = NULL;
static int (*real_openat64_fn)(int dirfd, const char *pathname, int flags, ...) = NULL;
static FILE *(*real_fopen_fn)(const char *pathname, const char *mode) = NULL;
static FILE *(*real_fopen64_fn)(const char *pathname, const char *mode) = NULL;
static DIR *(*real_opendir_fn)(const char *name) = NULL;
static int (*real_access_fn)(const char *pathname, int mode) = NULL;
static int (*real_stat_fn)(const char *pathname, struct stat *statbuf) = NULL;
static int (*real_lstat_fn)(const char *pathname, struct stat *statbuf) = NULL;
static int (*real_xstat_fn)(int ver, const char *pathname, struct stat *statbuf) = NULL;
static int (*real_lxstat_fn)(int ver, const char *pathname, struct stat *statbuf) = NULL;

static int safe_snprintf(char *out, size_t out_len, const char *fmt, ...) {
    va_list ap;
    int written = 0;
    va_start(ap, fmt);
    written = vsnprintf(out, out_len, fmt, ap);
    va_end(ap);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }
    return 0;
}

static int normalize_abs_path(const char *input, char *out, size_t out_len) {
    if (!input || !out || out_len == 0 || input[0] != '/') {
        return -1;
    }
    char work[PATH_MAX];
    if (safe_snprintf(work, sizeof(work), "%s", input) != 0) {
        return -1;
    }
    const size_t max_parts = PATH_MAX / 2;
    char *parts[PATH_MAX / 2];
    size_t part_count = 0;
    char *saveptr = NULL;
    char *token = strtok_r(work, "/", &saveptr);
    while (token) {
        if (strcmp(token, ".") == 0 || strcmp(token, "") == 0) {
            token = strtok_r(NULL, "/", &saveptr);
            continue;
        }
        if (strcmp(token, "..") == 0) {
            if (part_count > 0) {
                part_count -= 1;
            }
            token = strtok_r(NULL, "/", &saveptr);
            continue;
        }
        if (part_count < max_parts) {
            parts[part_count++] = token;
        }
        token = strtok_r(NULL, "/", &saveptr);
    }

    out[0] = '/';
    out[1] = '\0';
    for (size_t i = 0; i < part_count; i++) {
        size_t cur = strlen(out);
        if (cur + 1 >= out_len) {
            return -1;
        }
        if (cur > 1) {
            out[cur] = '/';
            out[cur + 1] = '\0';
            cur += 1;
        }
        size_t need = strlen(parts[i]);
        if (cur + need >= out_len) {
            return -1;
        }
        memcpy(out + cur, parts[i], need);
        out[cur + need] = '\0';
    }
    return 0;
}

static int resolve_dirfd(int dirfd, char *out, size_t out_len) {
    if (dirfd == AT_FDCWD) {
        if (!getcwd(out, out_len)) {
            return -1;
        }
        return 0;
    }
    char fd_path[64];
    if (safe_snprintf(fd_path, sizeof(fd_path), "/proc/self/fd/%d", dirfd) != 0) {
        return -1;
    }
    ssize_t n = readlink(fd_path, out, out_len - 1);
    if (n <= 0 || (size_t)n >= out_len) {
        return -1;
    }
    out[n] = '\0';
    return 0;
}

static int build_path_for_check(int dirfd, const char *pathname, char *out, size_t out_len) {
    if (!pathname || !out || out_len == 0) {
        return -1;
    }
    if (pathname[0] == '/') {
        return normalize_abs_path(pathname, out, out_len);
    }
    char base[PATH_MAX];
    if (resolve_dirfd(dirfd, base, sizeof(base)) != 0) {
        return -1;
    }
    char merged[PATH_MAX];
    if (safe_snprintf(merged, sizeof(merged), "%s/%s", base, pathname) != 0) {
        return -1;
    }
    return normalize_abs_path(merged, out, out_len);
}

static bool starts_with_prefix(const char *path, const char *prefix) {
    if (!path || !prefix) {
        return false;
    }
    size_t plen = strlen(prefix);
    if (plen == 0) {
        return false;
    }
    if (strcmp(prefix, "/") == 0) {
        return true;
    }
    if (strncmp(path, prefix, plen) != 0) {
        return false;
    }
    return path[plen] == '\0' || path[plen] == '/';
}

static void parse_prefix_env(const char *raw, PrefixList *list) {
    if (!raw || !*raw || !list) {
        return;
    }
    char *copy = strdup(raw);
    if (!copy) {
        return;
    }
    size_t capacity = 8;
    list->items = (char **)calloc(capacity, sizeof(char *));
    if (!list->items) {
        free(copy);
        return;
    }
    list->count = 0;
    char *saveptr = NULL;
    char *line = strtok_r(copy, "\n", &saveptr);
    while (line) {
        while (*line == ' ' || *line == '\t') {
            line++;
        }
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t')) {
            line[len - 1] = '\0';
            len -= 1;
        }
        if (len > 0 && line[0] == '/') {
            char norm[PATH_MAX];
            if (normalize_abs_path(line, norm, sizeof(norm)) == 0) {
                if (list->count == capacity) {
                    size_t next_capacity = capacity * 2;
                    char **next = (char **)realloc(list->items, next_capacity * sizeof(char *));
                    if (!next) {
                        break;
                    }
                    list->items = next;
                    capacity = next_capacity;
                }
                list->items[list->count] = strdup(norm);
                if (list->items[list->count]) {
                    list->count += 1;
                }
            }
        }
        line = strtok_r(NULL, "\n", &saveptr);
    }
    free(copy);
}

static void init_guard(void) {
    real_open_fn = dlsym(RTLD_NEXT, "open");
    real_open64_fn = dlsym(RTLD_NEXT, "open64");
    real_openat_fn = dlsym(RTLD_NEXT, "openat");
    real_openat64_fn = dlsym(RTLD_NEXT, "openat64");
    real_fopen_fn = dlsym(RTLD_NEXT, "fopen");
    real_fopen64_fn = dlsym(RTLD_NEXT, "fopen64");
    real_opendir_fn = dlsym(RTLD_NEXT, "opendir");
    real_access_fn = dlsym(RTLD_NEXT, "access");
    real_stat_fn = dlsym(RTLD_NEXT, "stat");
    real_lstat_fn = dlsym(RTLD_NEXT, "lstat");
    real_xstat_fn = dlsym(RTLD_NEXT, "__xstat");
    real_lxstat_fn = dlsym(RTLD_NEXT, "__lxstat");
    parse_prefix_env(getenv("POLYGONLIKE_PATH_GUARD_DENY_PREFIXES"), &g_deny);
    parse_prefix_env(getenv("POLYGONLIKE_PATH_GUARD_ALLOW_PREFIXES"), &g_allow);
}

static bool is_guarded_path(int dirfd, const char *pathname) {
    pthread_once(&g_once, init_guard);
    if (!pathname || g_deny.count == 0) {
        return false;
    }
    char normalized[PATH_MAX];
    if (build_path_for_check(dirfd, pathname, normalized, sizeof(normalized)) != 0) {
        return false;
    }
    for (size_t i = 0; i < g_allow.count; i++) {
        if (starts_with_prefix(normalized, g_allow.items[i])) {
            return false;
        }
    }
    for (size_t i = 0; i < g_deny.count; i++) {
        if (starts_with_prefix(normalized, g_deny.items[i])) {
            return true;
        }
    }
    return false;
}

static int deny_with_eacces_int(void) {
    errno = EACCES;
    return -1;
}

static FILE *deny_with_eacces_file(void) {
    errno = EACCES;
    return NULL;
}

static DIR *deny_with_eacces_dir(void) {
    errno = EACCES;
    return NULL;
}

int open(const char *pathname, int flags, ...) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    mode_t mode = 0;
    if ((flags & O_CREAT) || (flags & O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
        return real_open_fn ? real_open_fn(pathname, flags, mode) : deny_with_eacces_int();
    }
    return real_open_fn ? real_open_fn(pathname, flags) : deny_with_eacces_int();
}

int open64(const char *pathname, int flags, ...) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    mode_t mode = 0;
    if ((flags & O_CREAT) || (flags & O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
        if (real_open64_fn) {
            return real_open64_fn(pathname, flags, mode);
        }
        if (real_open_fn) {
            return real_open_fn(pathname, flags, mode);
        }
        return deny_with_eacces_int();
    }
    if (real_open64_fn) {
        return real_open64_fn(pathname, flags);
    }
    if (real_open_fn) {
        return real_open_fn(pathname, flags);
    }
    return deny_with_eacces_int();
}

int openat(int dirfd, const char *pathname, int flags, ...) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(dirfd, pathname)) {
        return deny_with_eacces_int();
    }
    mode_t mode = 0;
    if ((flags & O_CREAT) || (flags & O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
        return real_openat_fn ? real_openat_fn(dirfd, pathname, flags, mode) : deny_with_eacces_int();
    }
    return real_openat_fn ? real_openat_fn(dirfd, pathname, flags) : deny_with_eacces_int();
}

int openat64(int dirfd, const char *pathname, int flags, ...) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(dirfd, pathname)) {
        return deny_with_eacces_int();
    }
    mode_t mode = 0;
    if ((flags & O_CREAT) || (flags & O_TMPFILE)) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
        if (real_openat64_fn) {
            return real_openat64_fn(dirfd, pathname, flags, mode);
        }
        if (real_openat_fn) {
            return real_openat_fn(dirfd, pathname, flags, mode);
        }
        return deny_with_eacces_int();
    }
    if (real_openat64_fn) {
        return real_openat64_fn(dirfd, pathname, flags);
    }
    if (real_openat_fn) {
        return real_openat_fn(dirfd, pathname, flags);
    }
    return deny_with_eacces_int();
}

FILE *fopen(const char *pathname, const char *mode) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_file();
    }
    return real_fopen_fn ? real_fopen_fn(pathname, mode) : deny_with_eacces_file();
}

FILE *fopen64(const char *pathname, const char *mode) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_file();
    }
    if (real_fopen64_fn) {
        return real_fopen64_fn(pathname, mode);
    }
    if (real_fopen_fn) {
        return real_fopen_fn(pathname, mode);
    }
    return deny_with_eacces_file();
}

DIR *opendir(const char *name) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, name)) {
        return deny_with_eacces_dir();
    }
    return real_opendir_fn ? real_opendir_fn(name) : deny_with_eacces_dir();
}

int access(const char *pathname, int mode) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    return real_access_fn ? real_access_fn(pathname, mode) : deny_with_eacces_int();
}

int stat(const char *pathname, struct stat *statbuf) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    return real_stat_fn ? real_stat_fn(pathname, statbuf) : deny_with_eacces_int();
}

int lstat(const char *pathname, struct stat *statbuf) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    return real_lstat_fn ? real_lstat_fn(pathname, statbuf) : deny_with_eacces_int();
}

int __xstat(int ver, const char *pathname, struct stat *statbuf) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    return real_xstat_fn ? real_xstat_fn(ver, pathname, statbuf) : deny_with_eacces_int();
}

int __lxstat(int ver, const char *pathname, struct stat *statbuf) {
    pthread_once(&g_once, init_guard);
    if (is_guarded_path(AT_FDCWD, pathname)) {
        return deny_with_eacces_int();
    }
    return real_lxstat_fn ? real_lxstat_fn(ver, pathname, statbuf) : deny_with_eacces_int();
}
