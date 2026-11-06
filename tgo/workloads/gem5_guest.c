/* 无libc的AArch64 SE工作集。Linux系统调用仅用于输出和退出。 */
typedef unsigned long u64;
static long syscall3(long number,long a,long b,long c) {
    register long x0 __asm__("x0")=a;
    register long x1 __asm__("x1")=b;
    register long x2 __asm__("x2")=c;
    register long x8 __asm__("x8")=number;
    __asm__ volatile("svc #0":"+r"(x0):"r"(x1),"r"(x2),"r"(x8):"memory","cc");
    return x0;
}
static u64 decimal(const char *s) {u64 n=0;while(*s>='0'&&*s<='9')n=n*10+(*s++-'0');return n;}
__attribute__((noinline)) static u64 step(u64 x,u64 index) {
    switch ((x^index)&7) {
    case 0:return (x<<3)+(index^0x1234567);
    case 1:return (x>>3)^(x*17);
    case 2:return x*33+index;
    case 3:return (x^0x9e3779b9)+(x>>17);
    case 4:return (x<<9)^(x>>7)^index;
    case 5:return (x+0xabcdef01)*5;
    case 6:return (x>>11)+(x<<5)+index*7;
    default:return (x^index)*0x10203041;
    }
}
__attribute__((noreturn)) void guest_main(u64 *stack) {
    u64 argc=stack[0];char **argv=(char **)(stack+1);
    u64 count=argc>1?decimal(argv[1]):100000,seed=argc>2?decimal(argv[2]):101;
    u64 table[256],value=seed;
    for(u64 n=0;n<256;n++)table[n]=step(seed+n,n);
    for(u64 n=0;n<count;n++) {
        u64 i=(value+n)&255;
        value=step(value^table[i],n);
        table[i]=value+(table[(i+17)&255]>>3);
    }
    for(u64 n=0;n<256;n++)value^=table[n];
    char output[]="gem5-guest-ok 0000000000000000\n";
    for(u64 n=0;n<16;n++)output[14+n]="0123456789abcdef"[(value>>(60-4*n))&15];
    syscall3(64,1,(long)output,sizeof(output)-1);
    syscall3(93,0,0,0);for(;;){}
}
__attribute__((naked,noreturn)) void _start(void) {
    __asm__ volatile("mov x0, sp\nbl guest_main\nbrk #0");
}
