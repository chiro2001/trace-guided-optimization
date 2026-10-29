#ifndef TGO_SHA256_H
#define TGO_SHA256_H
/* SHA-256 compression and padding from FIPS 180-4. No external runtime library. */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <unistd.h>

typedef struct { uint32_t h[8]; uint64_t bytes; unsigned char block[64]; size_t used; } tgo_sha256;
static uint32_t tgo_rotr(uint32_t x,unsigned n) { return (x>>n)|(x<<(32-n)); }
static void tgo_sha_block(tgo_sha256 *s,const unsigned char *p) {
    static const uint32_t k[64]={
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    uint32_t w[64];
    for(unsigned i=0;i<16;i++) w[i]=((uint32_t)p[i*4]<<24)|((uint32_t)p[i*4+1]<<16)|((uint32_t)p[i*4+2]<<8)|p[i*4+3];
    for(unsigned i=16;i<64;i++) {
        uint32_t a=w[i-15],b=w[i-2];
        w[i]=w[i-16]+(tgo_rotr(a,7)^tgo_rotr(a,18)^(a>>3))+w[i-7]+(tgo_rotr(b,17)^tgo_rotr(b,19)^(b>>10));
    }
    uint32_t a=s->h[0],b=s->h[1],c=s->h[2],d=s->h[3],e=s->h[4],f=s->h[5],g=s->h[6],h=s->h[7];
    for(unsigned i=0;i<64;i++) {
        uint32_t t1=h+(tgo_rotr(e,6)^tgo_rotr(e,11)^tgo_rotr(e,25))+((e&f)^(~e&g))+k[i]+w[i];
        uint32_t t2=(tgo_rotr(a,2)^tgo_rotr(a,13)^tgo_rotr(a,22))+((a&b)^(a&c)^(b&c));
        h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static void tgo_sha_update(tgo_sha256 *s,const unsigned char *p,size_t n) {
    s->bytes+=n;
    while(n) {
        size_t take=64-s->used;if(take>n) take=n;
        memcpy(s->block+s->used,p,take);s->used+=take;p+=take;n-=take;
        if(s->used==64) { tgo_sha_block(s,s->block);s->used=0; }
    }
}
static int tgo_sha256_file(const char *path,unsigned char digest[32]) {
    tgo_sha256 s={{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19},0,{0},0};
    int fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0) return -1;
    unsigned char buffer[65536];ssize_t n;
    while((n=read(fd,buffer,sizeof(buffer)))!=0) {
        if(n<0) { if(errno==EINTR) continue;close(fd);return -1; }
        tgo_sha_update(&s,buffer,(size_t)n);
    }
    close(fd);
    uint64_t bits=s.bytes*8;
    unsigned char padding[128]={0x80};size_t size=s.used<56?56-s.used:120-s.used;
    for(unsigned i=0;i<8;i++) padding[size+i]=(unsigned char)(bits>>(56-i*8));
    tgo_sha_update(&s,padding,size+8);
    for(unsigned i=0;i<8;i++) for(unsigned j=0;j<4;j++) digest[i*4+j]=(unsigned char)(s.h[i]>>(24-j*8));
    return 0;
}
static int tgo_sha256_matches(const char *path,const unsigned char expected[32]) {
    unsigned char actual[32];return !tgo_sha256_file(path,actual) && !memcmp(actual,expected,32);
}
static inline int tgo_sha256_hex_matches(const char *path,const char *hex) {
    if(!hex || strlen(hex)!=64) return 0;
    unsigned char expected[32];
    for(unsigned i=0;i<32;i++) {
        unsigned value=0;
        for(unsigned j=0;j<2;j++) {
            unsigned char c=(unsigned char)hex[i*2+j];unsigned digit;
            if(c>='0' && c<='9') digit=c-'0';
            else if(c>='a' && c<='f') digit=c-'a'+10;
            else if(c>='A' && c<='F') digit=c-'A'+10;
            else return 0;
            value=(value<<4)|digit;
        }
        expected[i]=(unsigned char)value;
    }
    return tgo_sha256_matches(path,expected);
}
#endif
