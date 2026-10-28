#include <stdint.h>

#define FN(N) __attribute__((noinline,aligned(4096))) static uint64_t f##N(uint64_t x) { \
    if(__builtin_expect((x&65535)==65535,0)) { \
        for(unsigned k=0;k<100;k++) x=(x*1664525+1013904223)^N; \
    } return (x*33+N) ^ (x>>7); }
FN(0) FN(1) FN(2) FN(3) FN(4) FN(5) FN(6) FN(7)
FN(8) FN(9) FN(10) FN(11) FN(12) FN(13) FN(14) FN(15)
FN(16) FN(17) FN(18) FN(19) FN(20) FN(21) FN(22) FN(23)
FN(24) FN(25) FN(26) FN(27) FN(28) FN(29) FN(30) FN(31)
uint64_t frontend_work(uint64_t n) {
    uint64_t x=12345;
    for(uint64_t i=0;i<n;i++) {
        unsigned key=(unsigned)(i&31);
        switch(key) {
#define CASE(N) case N: x=f##N(x); break;
        CASE(0) CASE(1) CASE(2) CASE(3) CASE(4) CASE(5) CASE(6) CASE(7)
        CASE(8) CASE(9) CASE(10) CASE(11) CASE(12) CASE(13) CASE(14) CASE(15)
        CASE(16) CASE(17) CASE(18) CASE(19) CASE(20) CASE(21) CASE(22) CASE(23)
        CASE(24) CASE(25) CASE(26) CASE(27) CASE(28) CASE(29) CASE(30) CASE(31)
        }
    }
    return x;
}
