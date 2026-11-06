#include <atomic>
#include <cstdint>
#include <stdexcept>
#include <string>

static std::atomic<uint64_t> counter{0};
static thread_local uint64_t local_count=0;
struct Base { virtual uint64_t eval(uint64_t x) const=0; virtual ~Base()=default; };
struct Impl:Base { uint64_t eval(uint64_t x) const override { return (x*17)^0x12345678; } };
static Impl object;
static const Base* volatile selected=&object;
using Fn=uint64_t(*)(uint64_t);
static __attribute__((noinline)) uint64_t hidden_fn(uint64_t x) {return x+7;}
static Fn volatile saved_function=hidden_fn;
extern "C" __attribute__((noinline)) uint64_t rb_fixture_identity() {return saved_function==hidden_fn;}
extern "C" __attribute__((noinline)) uintptr_t rb_fixture_function_address() {return reinterpret_cast<uintptr_t>(hidden_fn);}

extern "C" __attribute__((noinline)) uint64_t rb_fixture_count() {return counter.load();}
extern "C" __attribute__((noinline)) uint64_t rb_fixture_tls() {return local_count;}
extern "C" __attribute__((noinline)) const void* rb_fixture_state() {return &counter;}
extern "C" __attribute__((noinline)) uint64_t rb_fixture_throw(uint64_t x) {
    if ((x&15)==7) throw std::runtime_error(std::to_string(x));
    return selected->eval(x);
}
extern "C" __attribute__((noinline)) uint64_t rb_fixture_step(uint64_t x) {
    ++counter;++local_count;
    uint64_t y;
    try {y=rb_fixture_throw(x);} catch(const std::runtime_error&) {y=x+999;}
    switch(x&7) {
        case 0:return y+3;case 1:return y^77;case 2:return y*3;case 3:return y+19;
        case 4:return y^55;case 5:return y-9;case 6:return y*5;default:return y+31;
    }
}
