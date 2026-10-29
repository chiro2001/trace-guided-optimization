#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "include/tgo_sha256.h"

__attribute__((used)) static const char tgo_launcher_abi[]="TGO_LAUNCHER_ABI_2";

/* Template built once; each entry carries a binary configuration after the ELF. */
static unsigned char *cursor,*end;
static void bad(void) { fputs("tgo: invalid entry configuration\n",stderr); exit(125); }
static uint64_t number(size_t n) {
    if(n>8 || (size_t)(end-cursor)<n) bad();
    uint64_t v=0; for(size_t i=0;i<n;i++) v|=(uint64_t)*cursor++<<(i*8); return v;
}
static char *string(void) {
    uint64_t n=number(4); if(n>16*1024*1024 || (uint64_t)(end-cursor)<n) bad();
    char *s=malloc((size_t)n+1); if(!s) bad();
    memcpy(s,cursor,(size_t)n); s[n]=0; cursor+=n; return s;
}
static void refresh_go_stamp(const char *path,const struct stat *st) {
    const char *keys[2]={"TGO_GO_ORIGINAL","TGO_GO_COMPILER"};
    const char *stamps[2]={"TGO_GO_ORIGINAL_STAMP","TGO_GO_COMPILER_STAMP"};
    for(unsigned i=0;i<2;i++) {
        const char *expected=getenv(keys[i]);
        if(expected && !strcmp(path,expected)) {
            char value[160];
            snprintf(value,sizeof(value),"%llu:%llu:%lld:%lld:%ld",(unsigned long long)st->st_dev,
                (unsigned long long)st->st_ino,(long long)st->st_size,(long long)st->st_mtim.tv_sec,st->st_mtim.tv_nsec);
            if(setenv(stamps[i],value,1)) bad();
        }
    }
}
int main(int argc,char **argv) {
    int fd=open("/proc/self/exe",O_RDONLY|O_CLOEXEC); struct stat st;
    unsigned char footer[16];
    if(fd<0 || fstat(fd,&st) || st.st_size<16 || pread(fd,footer,16,st.st_size-16)!=16) bad();
    cursor=footer;end=footer+16;
    uint64_t size=number(8);
    int format2=!memcmp(cursor,"TGOCFG02",8);
    if((!format2 && memcmp(cursor,"TGOCFG01",8)) || size>64*1024*1024 || size>(uint64_t)st.st_size-16) bad();
    unsigned char *data=malloc((size_t)size); if(!data) bad();
    if(pread(fd,data,(size_t)size,st.st_size-16-(off_t)size)!=(ssize_t)size) bad();
    close(fd); cursor=data;end=data+size;
    char *target=string(),*audit=string(),*map=string(),*cwd=string();
    size_t n=(size_t)number(4); if(!n || n>65536) bad();
    char **args=calloc(n+(size_t)argc,sizeof(char*)); if(!args) bad();
    for(size_t i=0;i<n;i++) args[i]=string();
    for(int i=1;i<argc;i++) args[n+(size_t)i-1]=argv[i];
    size_t ne=(size_t)number(4); if(ne>65536) bad();
    for(size_t i=0;i<ne;i++) { char *key=string(),*value=string(); if(setenv(key,value,1)) bad(); }
    const char *go_tool=getenv("TGO_GO_TOOLEXEC");
    if(go_tool && *go_tool) {
        const char *old=getenv("GOFLAGS");
        if(old && strstr(old,"-toolexec")) { fputs("tgo: existing -toolexec conflicts with compiler adapter\n",stderr); return 125; }
        size_t size=(old?strlen(old):0)+strlen(go_tool)+16;
        char *flags=malloc(size); if(!flags) bad();
        snprintf(flags,size,"%s%s-toolexec=%s",old?old:"",old&&*old?" ":"",go_tool);
        if(setenv("GOFLAGS",flags,1)) bad(); free(flags);
    }
    const char *validation_cache=getenv("TGO_VALIDATION_GOCACHE");
    if(validation_cache && *validation_cache && setenv("GOCACHE",validation_cache,1)) bad();
    const char *support=getenv("TGO_PYTHON_SUPPORT");
    if(support && *support) {
        const char *old=getenv("PYTHONPATH");
        size_t size=strlen(support)+(old?strlen(old):0)+2;
        char *path=malloc(size); if(!path) bad();
        snprintf(path,size,"%s%s%s",support,old&&*old?":":"",old?old:"");
        if(setenv("PYTHONPATH",path,1)) bad(); free(path);
    }
    int disabled=getenv("TGO_DISABLE") && !strcmp(getenv("TGO_DISABLE"),"1");
    size_t nc=(size_t)number(4); if(nc>1024) bad();
    for(size_t i=0;i<nc;i++) {
        char *path=string(); int64_t sz=(int64_t)number(8),sec=(int64_t)number(8),ns=(int64_t)number(8);
        uint64_t ino=number(8),dev=number(8);
        const unsigned char *digest=NULL;
        if(format2) { if(end-cursor<32) bad();digest=cursor;cursor+=32; }
        int same=!stat(path,&st) && st.st_size==sz && st.st_mtim.tv_sec==sec &&
            st.st_mtim.tv_nsec==ns && st.st_ino==ino && st.st_dev==dev;
        if(!disabled && !same && (!format2 || !tgo_sha256_matches(path,digest))) {
            fprintf(stderr,"tgo: ELF changed: %s; rebuild or use TGO_DISABLE=1\n",path); return 125;
        }
        if(!disabled && !stat(path,&st)) refresh_go_stamp(path,&st);
    }
    if(cursor!=end) bad();
    if(!disabled && *audit) {
        const char *prior=getenv("LD_AUDIT");
        if(prior && *prior) { fputs("tgo: existing LD_AUDIT would conflict\n",stderr); return 125; }
        setenv("TGO_REDIRECT",map,1); setenv("LD_AUDIT",audit,1);
    }
    if(*cwd && chdir(cwd)) { perror("tgo: chdir"); return 125; }
    execv(disabled?args[0]:target,args); perror("tgo: execv"); return 126;
}
