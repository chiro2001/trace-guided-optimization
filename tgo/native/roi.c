#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define START 0x51554d4153544152ULL
#define STOP  0x51554d4153544f50ULL
static __thread unsigned depth;
static void marker(int begin) {
    const char *control=getenv("TGO_PERF_CONTROL");
    if(control) {
        int fd=open(control,O_WRONLY|O_NONBLOCK|O_CLOEXEC);
        if(fd<0) { perror("tgo: perf control"); _exit(125); }
        const char *msg=begin?"enable\n":"disable\n";
        ssize_t r=write(fd,msg,strlen(msg)); close(fd);
        if(r!=(ssize_t)strlen(msg)) _exit(125);
        const char *ack=getenv("TGO_PERF_ACK");
        if(ack) {
            fd=open(ack,O_RDONLY|O_CLOEXEC);
            if(fd<0) _exit(125);
            char b[16]; r=read(fd,b,sizeof(b)); close(fd);
            if(r<4 || memcmp(b,"ack",3)) _exit(125);
        }
    }
    const char *prefix=getenv("TGO_ROI_LOG");
    if(prefix) {
        char path[4096], row[160]; struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC,&ts);
        snprintf(path,sizeof(path),"%s.%ld.roi",prefix,(long)getpid());
        int fd=open(path,O_CREAT|O_WRONLY|O_APPEND|O_CLOEXEC,0600);
        if(fd>=0) {
            int n=snprintf(row,sizeof(row),"%c\t%ld\t%lld\n",begin?'B':'E',
                (long)syscall(SYS_gettid),(long long)ts.tv_sec*1000000000LL+ts.tv_nsec);
            ssize_t r=write(fd,row,(size_t)n); (void)r; close(fd);
        }
    }
    syscall(SYS_getpid,begin?START:STOP,0);
}
void tgo_roi_begin(void) { if(depth++==0) marker(1); }
void tgo_roi_end(void) { if(!depth) { fputs("tgo: unbalanced ROI\n",stderr); _exit(125); } if(--depth==0) marker(0); }
