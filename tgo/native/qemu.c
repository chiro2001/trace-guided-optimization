/* Aggregated per-vCPU translation-block execution. No instruction trace on disk. */
#include <qemu-plugin.h>
#include <glib.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
typedef struct { uint64_t start,end; unsigned last_size,n; } Block;
typedef struct { uint64_t a,b; char kind; } Key;
typedef struct { Block prev; int valid,active; } CPU;
static GHashTable *counts;
static GMutex lock;
static CPU cpus[4096];
static uint64_t insns,limit=1000000000,windows;
static int whole=1,truncated;
static const char *output;
static int getpid_nr;

static guint hash(gconstpointer p) { const Key *k=p; return (guint)(k->a^(k->a>>32)^k->b^(k->b>>32)^k->kind); }
static gboolean equal(gconstpointer a,gconstpointer b) { const Key *x=a,*y=b; return x->a==y->a && x->b==y->b && x->kind==y->kind; }
static void add(char kind,uint64_t a,uint64_t b,uint64_t n) {
    Key key={a,b,kind}; uint64_t *v=g_hash_table_lookup(counts,&key);
    if(v) *v+=n;
    else { Key *k=g_new(Key,1); *k=key; v=g_new(uint64_t,1); *v=n; g_hash_table_insert(counts,k,v); }
}
static void exec_tb(unsigned int cpu,void *userdata) {
    if(cpu>=4096) return;
    Block *b=userdata; g_mutex_lock(&lock); CPU *c=&cpus[cpu];
    if(!(whole||c->active) || truncated) { c->valid=0; g_mutex_unlock(&lock); return; }
    if(insns+b->n>limit) { truncated=1; c->valid=0; g_mutex_unlock(&lock); return; }
    insns+=b->n;
    add('T',b->start,b->end,1);
    add('N',b->start,b->end,b->n);
    if(c->valid && b->start!=c->prev.end+c->prev.last_size) add('B',c->prev.end,b->start,1);
    c->prev=*b; c->valid=1;
    g_mutex_unlock(&lock);
}
#if QEMU_PLUGIN_VERSION >= 7
static void translate(struct qemu_plugin_tb *tb,void *unused) {
    (void)unused;
#else
static void translate(qemu_plugin_id_t id,struct qemu_plugin_tb *tb) {
    (void)id;
#endif
    size_t n=qemu_plugin_tb_n_insns(tb); if(!n) return;
    Block *b=g_new0(Block,1);
    struct qemu_plugin_insn *last=qemu_plugin_tb_get_insn(tb,n-1);
    b->start=qemu_plugin_insn_vaddr(qemu_plugin_tb_get_insn(tb,0));
    b->end=qemu_plugin_insn_vaddr(last); b->last_size=qemu_plugin_insn_size(last); b->n=(unsigned)n;
    qemu_plugin_register_vcpu_tb_exec_cb(tb,exec_tb,QEMU_PLUGIN_CB_NO_REGS,b);
}
static void syscall_cb(unsigned int cpu,int64_t num,uint64_t a1,uint64_t a2,uint64_t a3,
                       uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7,uint64_t a8
#if QEMU_PLUGIN_VERSION >= 7
                       ,void *unused
#endif
                       ) {
    (void)a2;(void)a3;(void)a4;(void)a5;(void)a6;(void)a7;(void)a8;
#if QEMU_PLUGIN_VERSION >= 7
    (void)unused;
#endif
    if(num!=getpid_nr || cpu>=4096) return;
    g_mutex_lock(&lock);
    if(a1==UINT64_C(0x51554d4153544152)) { cpus[cpu].active=1; cpus[cpu].valid=0; windows++; }
    if(a1==UINT64_C(0x51554d4153544f50)) { cpus[cpu].active=0; cpus[cpu].valid=0; }
    g_mutex_unlock(&lock);
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
    fprintf(f,"H\t%"PRIu64"\t%d\t%"PRIu64"\n",insns,truncated,windows);
    GHashTableIter it; gpointer kp,vp; g_hash_table_iter_init(&it,counts);
    while(g_hash_table_iter_next(&it,&kp,&vp)) {
        Key *k=kp; fprintf(f,"%c\t%"PRIx64"\t%"PRIx64"\t%"PRIu64"\n",k->kind,k->a,k->b,*(uint64_t*)vp);
    }
    fclose(f);
}
QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,const qemu_info_t *info,int argc,char **argv) {
    output="tgo"; getpid_nr=!strcmp(info->target_name,"aarch64")?172:39;
    for(int i=0;i<argc;i++) {
        if(!strncmp(argv[i],"out=",4)) output=g_strdup(argv[i]+4);
        else if(!strcmp(argv[i],"roi=marker")) whole=0;
        else if(!strncmp(argv[i],"limit=",6)) limit=strtoull(argv[i]+6,NULL,10);
        else return -1;
    }
    counts=g_hash_table_new_full(hash,equal,g_free,g_free); g_mutex_init(&lock);
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
