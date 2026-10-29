#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "include/tgo_sha256.h"

/* Go's -toolexec ABI: argv[1] is the original tool, argv[2:] its arguments.
 * Only the exact compiler recorded in the manifest may be substituted. */
static int disabled(void) {
    const char *v=getenv("TGO_DISABLE"); return v && !strcmp(v,"1");
}
static int check_stamp(const char *path,const char *text,const char *digest) {
    if(!text) return 0;
    unsigned long long dev,ino; long long size,sec; long ns; struct stat s;
    if(sscanf(text,"%llu:%llu:%lld:%lld:%ld",&dev,&ino,&size,&sec,&ns)==5 && !stat(path,&s) &&
        s.st_dev==dev && s.st_ino==ino && s.st_size==size &&
        s.st_mtim.tv_sec==sec && s.st_mtim.tv_nsec==ns) return 0;
    return tgo_sha256_hex_matches(path,digest)?0:-1;
}
int main(int argc,char **argv) {
    if(argc<2) { fputs("tgo: -toolexec requires an original tool path\n",stderr); return 125; }
    char original[PATH_MAX],expected[PATH_MAX];
    const char *source=getenv("TGO_GO_ORIGINAL");
    int selected=source && realpath(argv[1],original) && realpath(source,expected) && !strcmp(original,expected);
    if(!selected || disabled()) { execv(argv[1],argv+1); perror("tgo: exec Go tool"); return 126; }
    if(check_stamp(source,getenv("TGO_GO_ORIGINAL_STAMP"),getenv("TGO_GO_ORIGINAL_SHA256"))) {
        fputs("tgo: original Go compiler changed; retrain\n",stderr); return 125;
    }
    const char *compiler=getenv("TGO_GO_COMPILER");
    if(!compiler || !*compiler) compiler=argv[1];
    if(check_stamp(compiler,getenv("TGO_GO_COMPILER_STAMP"),getenv("TGO_GO_COMPILER_SHA256"))) {
        fputs("tgo: optimized Go compiler changed; rebuild\n",stderr); return 125;
    }
    if(argc==3 && !strcmp(argv[2],"-V=full")) {
        const char *version=getenv("TGO_GO_VERSION"),*id=getenv("TGO_GO_ID");
        if(version && id) {
            /* cmd/go uses the entire release line, or final buildID for devel.
             * Thus the cache identity includes our compiler's actual SHA256. */
            printf("%s %s\n",version,id); return 0;
        }
    }
    const char *log=getenv("TGO_GO_EXEC_LOG");
    if(log) {
        int fd=open(log,O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC,0600);
        if(fd<0) { perror("tgo: Go execution log"); return 125; }
        char row[2*PATH_MAX+64];
        int n=snprintf(row,sizeof(row),"%ld\t%s\t%s\n",(long)getpid(),source,compiler);
        if(n<=0 || (size_t)n>=sizeof(row) || write(fd,row,(size_t)n)!=n) { close(fd); return 125; }
        close(fd);
    }
    const char *profiles=getenv("TGO_GO_PROFILE_DIR");
    char profile[PATH_MAX+32];
    char **args=calloc((size_t)argc+1,sizeof(char*)); if(!args) return 125;
    args[0]=argv[1]; size_t count=1;
    if(profiles && *profiles) {
        int n=snprintf(profile,sizeof(profile),"-cpuprofile=%s/%ld.pprof",profiles,(long)getpid());
        if(n<0 || (size_t)n>=sizeof(profile)) return 125;
        args[count++]=profile;
    }
    for(int i=2;i<argc;i++) args[count++]=argv[i];
    execv(compiler,args); perror("tgo: exec compiler"); return 126;
}
