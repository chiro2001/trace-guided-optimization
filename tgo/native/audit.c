#define _GNU_SOURCE
#include <link.h>
#include <limits.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* No symbol-binding callbacks: this only redirects paths and records load addresses. */
struct redirect { char source[PATH_MAX], dest[PATH_MAX]; };
static struct redirect mappings[256];
static size_t count;

unsigned int la_version(unsigned int version) {
    (void)version;
    const char *p = getenv("TGO_REDIRECT");
    if (p) {
        FILE *f=fopen(p,"r");
        char line[2*PATH_MAX+4];
        if (!f) { fputs("tgo: cannot open redirect map\n",stderr); _exit(125); }
        while (fgets(line,sizeof(line),f)) {
            char *tab=strchr(line,'\t');
            if (!tab || count>=256) _exit(125);
            *tab++=0; tab[strcspn(tab,"\n")]=0;
            if (strlen(line)>=PATH_MAX || strlen(tab)>=PATH_MAX) _exit(125);
            strcpy(mappings[count].source,line); strcpy(mappings[count].dest,tab); count++;
        }
        fclose(f);
    }
    return LAV_CURRENT;
}

char *la_objsearch(const char *name, uintptr_t *cookie, unsigned int flag) {
    (void)cookie; (void)flag;
    if (strchr(name,'/')) {
        char path[PATH_MAX];
        if (realpath(name,path)) {
            for (size_t i=0;i<count;i++)
                if (!strcmp(path,mappings[i].source)) return mappings[i].dest;
        }
    }
    return (char*)name;
}

unsigned int la_objopen(struct link_map *map, Lmid_t ns, uintptr_t *cookie) {
    (void)ns; (void)cookie;
    const char *prefix=getenv("TGO_IMAGE_LOG");
    if (prefix) {
        char file[PATH_MAX], path[PATH_MAX], line[2*PATH_MAX+80];
        const char *name=map->l_name;
        if (!*name) {
            ssize_t n=readlink("/proc/self/exe",path,sizeof(path)-1);
            if(n<0) return 0; path[n]=0; name=path;
        } else {
            if(realpath(name,path)) name=path;
        }
        if (*name!='/') return 0;
        snprintf(file,sizeof(file),"%s.%ld.images",prefix,(long)getpid());
        int fd=open(file,O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC,0600);
        if(fd>=0) {
            int n=snprintf(line,sizeof(line),"M\t%lx\t%s\n",(unsigned long)map->l_addr,name);
            if(n>0 && (size_t)n<sizeof(line)) { ssize_t r=write(fd,line,(size_t)n); (void)r; }
            close(fd);
        }
    }
    return 0;
}
