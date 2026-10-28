#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct check { const char *path; long long size, sec; long ns; unsigned long long ino, dev; };
#include "launcher_config.h"

int main(int argc, char **argv) {
    int disabled = getenv("TGO_DISABLE") && !strcmp(getenv("TGO_DISABLE"), "1");
    if (!disabled) {
        for (size_t i=0; i<sizeof(checks)/sizeof(checks[0]); i++) {
            struct stat s;
            const struct check *c = &checks[i];
            if (stat(c->path, &s) || s.st_size != c->size || s.st_mtim.tv_sec != c->sec ||
                s.st_mtim.tv_nsec != c->ns || s.st_ino != c->ino || s.st_dev != c->dev) {
                fprintf(stderr,"tgo: ELF changed: %s; rebuild or use TGO_DISABLE=1\n",c->path);
                return 125;
            }
        }
        const char *prior = getenv("LD_AUDIT");
        if (prior && *prior) { fputs("tgo: existing LD_AUDIT would conflict\n",stderr); return 125; }
        setenv("TGO_REDIRECT", map_path, 1);
        setenv("LD_AUDIT", audit_path, 1);
    }
    for (size_t i=0; env_keys[i]; i++) setenv(env_keys[i], env_values[i], 1);
    if (workdir && chdir(workdir)) { perror("tgo: chdir"); return 125; }
    size_t n = sizeof(fixed)/sizeof(fixed[0])-1;
    char **args = calloc(n + (size_t)argc, sizeof(char*));
    if (!args) return 125;
    for (size_t i=0; i<n; i++) args[i]=(char*)fixed[i];
    for (int i=1; i<argc; i++) args[n+(size_t)i-1]=argv[i];
    execv(disabled ? fixed[0] : target, args);
    perror("tgo: execv");
    return 126;
}
