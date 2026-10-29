/* Aggregated per-vCPU translation-block execution. No instruction trace on disk. */
#include <qemu-plugin.h>
#include <pthread.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
typedef struct { uint64_t start,end; unsigned last_size,n; } Block;
typedef struct { uint64_t a,b; char kind; } Key;
typedef struct { Block prev; uint64_t run_start; int valid,active; } CPU;
typedef struct Count { Key key; uint64_t value; struct Count *next; } Count;
static Count *counts[65536];
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static CPU cpus[4096];
static uint64_t insns,limit=1000000000,windows;
static uint64_t window_limit;
static int whole=1,truncated;
static const char *output;
static int getpid_nr;
static struct qemu_plugin_scoreboard *active_score;
static qemu_plugin_u64 active_entry;

static void add(char kind,uint64_t a,uint64_t b,uint64_t n) {
    unsigned hash=(unsigned)(a^(a>>32)^(b<<5)^(b>>32)^(unsigned)kind)&65535;
    for(Count *c=counts[hash];c;c=c->next)
        if(c->key.a==a && c->key.b==b && c->key.kind==kind) { c->value+=n; return; }
    Count *c=calloc(1,sizeof(*c)); if(!c) { truncated=1; return; }
    c->key=(Key){a,b,kind}; c->value=n; c->next=counts[hash]; counts[hash]=c;
}
static void exec_tb(unsigned int cpu,void *userdata) {
    if(cpu>=4096) return;
    Block *b=userdata; pthread_mutex_lock(&lock); CPU *c=&cpus[cpu];
    if(!(whole||c->active) || truncated) { c->valid=0; pthread_mutex_unlock(&lock); return; }
    if(insns+b->n>limit) { truncated=1; if(c->valid) add('F',c->run_start,c->prev.end,1); c->valid=0; qemu_plugin_u64_set(active_entry,cpu,0); pthread_mutex_unlock(&lock); return; }
    insns+=b->n;
    add('N',b->start,b->end,b->n);
    if(c->valid && b->start!=c->prev.end+c->prev.last_size) {
        add('F',c->run_start,c->prev.end,1);
        add('B',c->prev.end,b->start,1); c->run_start=b->start;
    } else if(!c->valid) c->run_start=b->start;
    c->prev=*b; c->valid=1;
    pthread_mutex_unlock(&lock);
}
#if QEMU_PLUGIN_VERSION >= 7
static void translate(struct qemu_plugin_tb *tb,void *unused) {
    (void)unused;
#else
static void translate(qemu_plugin_id_t id,struct qemu_plugin_tb *tb) {
    (void)id;
#endif
    size_t n=qemu_plugin_tb_n_insns(tb); if(!n) return;
    Block *b=calloc(1,sizeof(*b)); if(!b) return;
    struct qemu_plugin_insn *last=qemu_plugin_tb_get_insn(tb,n-1);
    b->start=qemu_plugin_insn_vaddr(qemu_plugin_tb_get_insn(tb,0));
    b->end=qemu_plugin_insn_vaddr(last); b->last_size=qemu_plugin_insn_size(last); b->n=(unsigned)n;
    if(whole) qemu_plugin_register_vcpu_tb_exec_cb(tb,exec_tb,QEMU_PLUGIN_CB_NO_REGS,b);
    else qemu_plugin_register_vcpu_tb_exec_cond_cb(tb,exec_tb,QEMU_PLUGIN_CB_NO_REGS,
                                                  QEMU_PLUGIN_COND_EQ,active_entry,1,b);
}
static void syscall_cb(
#if QEMU_PLUGIN_VERSION < 7
                       qemu_plugin_id_t id,
#endif
                       unsigned int cpu,int64_t num,uint64_t a1,uint64_t a2,uint64_t a3,
                       uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7,uint64_t a8
#if QEMU_PLUGIN_VERSION >= 7
                       ,void *unused
#endif
                       ) {
    (void)a2;(void)a3;(void)a4;(void)a5;(void)a6;(void)a7;(void)a8;
#if QEMU_PLUGIN_VERSION >= 7
    (void)unused;
#else
    (void)id;
#endif
    if(num!=getpid_nr || cpu>=4096) return;
    pthread_mutex_lock(&lock);
    if(a1==UINT64_C(0x51554d4153544152)) {
        int collect=!window_limit || windows<window_limit;
        cpus[cpu].active=collect; cpus[cpu].valid=0;
        if(collect) windows++;
        qemu_plugin_u64_set(active_entry,cpu,collect);
    }
    if(a1==UINT64_C(0x51554d4153544f50)) {
        if(cpus[cpu].valid) add('F',cpus[cpu].run_start,cpus[cpu].prev.end,1);
        cpus[cpu].active=0; cpus[cpu].valid=0; qemu_plugin_u64_set(active_entry,cpu,0);
    }
    pthread_mutex_unlock(&lock);
}
#if QEMU_PLUGIN_VERSION >= 7
static void finish(void *userdata) {
    (void)userdata;
#else
static void finish(qemu_plugin_id_t id,void *userdata) {
    (void)id;(void)userdata;
#endif
    char path[4096]; snprintf(path,sizeof(path),"%s.%ld.trace",output,(long)getpid());
    FILE *f=fopen(path,"w"); if(!f) { perror("tgo: trace output"); return; }
    for(unsigned i=0;i<4096;i++) if(cpus[i].valid) add('F',cpus[i].run_start,cpus[i].prev.end,1);
    fprintf(f,"H\t%"PRIu64"\t%d\t%"PRIu64"\n",insns,truncated,windows);
    for(unsigned i=0;i<65536;i++) for(Count *c=counts[i];c;c=c->next) {
        Key *k=&c->key; fprintf(f,"%c\t%"PRIx64"\t%"PRIx64"\t%"PRIu64"\n",k->kind,k->a,k->b,c->value);
    }
    fclose(f);
}
QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,const qemu_info_t *info,int argc,char **argv) {
    output="tgo"; getpid_nr=!strcmp(info->target_name,"aarch64")?172:39;
    for(int i=0;i<argc;i++) {
        if(!strncmp(argv[i],"out=",4)) output=strdup(argv[i]+4);
        else if(!strcmp(argv[i],"roi=marker")) whole=0;
        else if(!strncmp(argv[i],"limit=",6)) limit=strtoull(argv[i]+6,NULL,10);
        else if(!strncmp(argv[i],"windows=",8)) window_limit=strtoull(argv[i]+8,NULL,10);
        else return -1;
    }
    active_score=qemu_plugin_scoreboard_new(sizeof(uint64_t));
    active_entry=qemu_plugin_scoreboard_u64(active_score);
#if QEMU_PLUGIN_VERSION >= 7
    qemu_plugin_register_vcpu_tb_trans_cb(id,translate,NULL);
    qemu_plugin_register_vcpu_syscall_cb(id,syscall_cb,NULL);
#else
    qemu_plugin_register_vcpu_tb_trans_cb(id,translate);
    qemu_plugin_register_vcpu_syscall_cb(id,syscall_cb);
#endif
    qemu_plugin_register_atexit_cb(id,finish,NULL);
    return 0;
}
