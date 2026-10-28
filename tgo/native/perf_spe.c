/* SPDX-License-Identifier: GPL-2.0 */
/* Project a decoded SPE branch sample into a single BOLT branch-stack entry.
 * perf 6.14 decodes all branch samples but does not yet fill brstack itself.
 * Opcode bytes come from perf's mmap-aware object-code API, never a nearby PC.
 * Prediction is explicitly unknown unless perf names the event branch-miss. */
#define _GNU_SOURCE
#include "perf_dlfilter.h"
#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct perf_dlfilter_fns perf_dlfilter_fns;
static uint64_t total, emitted, no_code, unsupported;

int filter_event(void *data,const struct perf_dlfilter_sample *s,void *ctx) {
    (void)data;
    if(s->size < offsetof(struct perf_dlfilter_sample,event)+sizeof(s->event)) return -1;
    if(!s->event || !strstr(s->event,"branch")) return 1;
    total++;
    uint32_t word=0;
    if(!perf_dlfilter_fns.object_code ||
       perf_dlfilter_fns.object_code(ctx,s->ip,&word,sizeof(word))!=(int)sizeof(word)) {
        no_code++; return 1;
    }
    int conditional=(word&0xff000000U)==0x54000000U ||
                    (word&0x7e000000U)==0x34000000U ||
                    (word&0x7e000000U)==0x36000000U;
    const char *type="-";
    if(conditional) type="COND";
    else if((word&0x7c000000U)==0x14000000U) type=(word&0x80000000U)?"CALL":"UNCOND";
    else if((word&0xfffffc1fU)==0xd61f0000U) type="IND";
    else if((word&0xfffffc1fU)==0xd63f0000U) type="IND_CALL";
    else if((word&0xfffffc1fU)==0xd65f0000U) type="RET";
    else { unsupported++; return 1; }
    char prediction=strstr(s->event,"miss")?'M':'-';
    int not_taken=conditional && s->addr==s->ip+4;
    printf("%d 0x%"PRIx64"/0x%"PRIx64"/%c%s/-/-/0/%s/-\n",
           s->pid,(uint64_t)s->ip,(uint64_t)s->addr,prediction,not_taken?"N":"",type);
    emitted++;
    return 1; /* Suppress perf's normal sample row; stdout is now brstack text. */
}

int stop(void *data,void *ctx) {
    (void)data;(void)ctx;
    const char *path=getenv("TGO_SPE_STATS");
    if(path) {
        FILE *f=fopen(path,"w");
        if(!f) return -1;
        fprintf(f,"{\"branch_samples\":%"PRIu64",\"emitted\":%"PRIu64
                ",\"unavailable_code\":%"PRIu64",\"unsupported_opcode\":%"PRIu64
                ",\"prediction\":\"unknown except named miss events\"}\n",
                total,emitted,no_code,unsupported);
        fclose(f);
    }
    return emitted?0:-1;
}

const char *filter_description(const char **detail) {
    *detail="SPE source/target with exact mapped AArch64 opcode checks; unknown prediction is retained.";
    return "TGO SPE to BOLT brstack compatibility bridge";
}
