#define _GNU_SOURCE
#include <stdint.h>
#include <stddef.h>
#include <stdatomic.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <dirent.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <linux/membarrier.h>

static int sync_membarrier;
static inline void local_sync_core(void) {
#if defined(__aarch64__)
    __asm__ volatile("dsb ish\n\tisb" ::: "memory");
#else
    __asm__ volatile("" ::: "memory");
#endif
}


typedef int (*rb_request_callback)(const char *, size_t);
typedef int (*rb_measure_callback_fn)(void *);
int rb_measure_callback(int ready_fd, int release_fd, rb_measure_callback_fn callback,
                        void *context, uint64_t *times) {
    char byte = 'R';
    ssize_t result;
    if (ready_fd >= 0) {
        do { result=write(ready_fd,&byte,1); } while(result<0 && errno==EINTR);
        if(result!=1) return -1;
    }
    if (release_fd >= 0) {
        do { result=read(release_fd,&byte,1); } while(result<0 && errno==EINTR);
        if(result!=1) return -1;
    }
    struct timespec start,end;
    if(clock_gettime(CLOCK_MONOTONIC,&start)) return -1;
    int status=callback(context);
    if(clock_gettime(CLOCK_MONOTONIC,&end)) return -1;
    times[0]=(uint64_t)start.tv_sec*1000000000ULL+(uint64_t)start.tv_nsec;
    times[1]=(uint64_t)end.tv_sec*1000000000ULL+(uint64_t)end.tv_nsec;
    return status;
}
int rb_fifo_loop(int fd, rb_request_callback callback) {
    size_t size = 4096, used = 0;
    char *buffer = malloc(size);
    if (!buffer) return -1;
    for (;;) {
        char byte;
        ssize_t n = read(fd, &byte, 1);
        if (n < 0 && errno == EINTR) continue;
        if (n != 1) { free(buffer); return -1; }
        if (byte == '\n') {
            int result = callback(buffer, used);
            used = 0;
            if (result <= 0) { free(buffer); return result; }
            continue;
        }
        if (used == size) {
            if (size >= 1048576) { free(buffer); errno = E2BIG; return -1; }
            size *= 2;
            char *next = realloc(buffer, size);
            if (!next) { free(buffer); return -1; }
            buffer = next;
        }
        buffer[used++] = byte;
    }
}

#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif
extern void __register_frame(const void *);
struct frame_bases { void *tbase, *dbase, *func; };
extern const void *_Unwind_Find_FDE(const void *, struct frame_bases *);

int rb_bind_node(int node) {
    unsigned long mask = 1UL << node;
    return (int)syscall(SYS_set_mempolicy, 2, &mask, 64);
}

void *rb_reserve(uint64_t address, size_t size) {
    void *p = mmap((void *)address, size, PROT_NONE,
                   MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED_NOREPLACE, -1, 0);
    return p == MAP_FAILED ? NULL : p;
}

int rb_prepare(uint64_t address, size_t size) {
    return mprotect((void *)address, size, PROT_READ|PROT_WRITE);
}

int rb_publish(uint64_t address, size_t size) {
    __builtin___clear_cache((char *)address, (char *)(address + size));
    if (mprotect((void *)address, size, PROT_READ|PROT_EXEC)) return -1;
    local_sync_core();
    return 0;
}

void *rb_register_frame(const void *data, size_t size) {
    size_t page = (size + 4095) & ~(size_t)4095;
    void *p = mmap(NULL, page, PROT_READ|PROT_WRITE,
                   MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return NULL;
    memcpy(p, data, size);
    if (mprotect(p, page, PROT_READ)) return NULL;
    __register_frame(p);
    return p;
}

uint64_t rb_find_frame(uint64_t pc) {
    struct frame_bases bases = {0};
    if (!_Unwind_Find_FDE((const void *)pc, &bases)) return 0;
    return (uint64_t)bases.func;
}

/* BOLT的pcrel CFI必须在其链接地址登记，不能复制到另一处后直接使用。 */
void rb_register_frame_at(uint64_t address) {
    __register_frame((const void *)address);
}

static _Atomic unsigned acknowledgements;
static int sync_signal;
static void sync_handler(int sig) {
    (void)sig;
    local_sync_core();
    atomic_fetch_add_explicit(&acknowledgements, 1, memory_order_release);
}

int rb_sync_method(void) { return sync_membarrier; }

int rb_install_sync(void) {
#ifdef SYS_membarrier
    long supported = syscall(SYS_membarrier, MEMBARRIER_CMD_QUERY, 0, 0);
    if ((supported & MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE) &&
        syscall(SYS_membarrier, MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE, 0, 0) == 0) {
        sync_membarrier = 1; return 0;
    }
#endif
    sync_signal = SIGRTMIN + 10;
    struct sigaction old, action;
    if (sigaction(sync_signal, NULL, &old)) return -1;
    if (old.sa_handler != SIG_DFL && old.sa_handler != sync_handler) {
        errno = EBUSY; return -1;
    }
    memset(&action, 0, sizeof(action));
    action.sa_handler = sync_handler;
    action.sa_flags = SA_RESTART;
    sigemptyset(&action.sa_mask);
    return sigaction(sync_signal, &action, NULL);
}

int rb_sync_threads(void) {
#ifdef SYS_membarrier
    if (sync_membarrier) return (int)syscall(SYS_membarrier, MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE, 0, 0);
#endif
    if (!sync_signal) { errno = EINVAL; return -1; }
    DIR *dir = opendir("/proc/self/task");
    if (!dir) return -1;
    unsigned expected = 0;
    pid_t self = (pid_t)syscall(SYS_gettid);
    atomic_store_explicit(&acknowledgements, 0, memory_order_relaxed);
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        pid_t tid = (pid_t)strtol(entry->d_name, NULL, 10);
        if (tid <= 0 || tid == self) continue;
        if (syscall(SYS_tgkill, getpid(), tid, sync_signal) == 0) ++expected;
        else if (errno != ESRCH) { closedir(dir); return -1; }
    }
    closedir(dir);
    struct timespec start, now, delay = {0, 1000000};
    clock_gettime(CLOCK_MONOTONIC, &start);
    while (atomic_load_explicit(&acknowledgements, memory_order_acquire) < expected) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        if ((now.tv_sec-start.tv_sec)*1000000000LL+now.tv_nsec-start.tv_nsec > 3000000000LL) {
            errno = ETIMEDOUT; return -1;
        }
        nanosleep(&delay, NULL);
    }
    local_sync_core();
    return (int)expected;
}
