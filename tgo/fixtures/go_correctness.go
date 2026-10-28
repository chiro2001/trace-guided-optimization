package main

import (
	"fmt"
	"math/bits"
	"runtime"
	"sort"
	"sync"
)

type number interface{ ~int | ~int64 }
func sum[T number](values []T) T { var result T; for _, v := range values { result += v }; return result }
type evaluator interface{ eval(int) int }
type node struct{ x int }
func (n node) eval(y int) int { return n.x+y }
func recoverCheck() (ok bool) { defer func(){ ok=recover()=="expected" }(); panic("expected") }

func main() {
	values:=[]int{9,1,7,3,5}; sort.Ints(values)
	if sum(values)!=25 || values[0]!=1 || bits.OnesCount64(0xf0f0)!=8 || !recoverCheck(){ panic("scalar/generic/exception") }
	var e evaluator=node{17}; if e.eval(25)!=42 { panic("interface") }
	var wg sync.WaitGroup; results:=make([]int,16)
	for i:=range results { wg.Add(1); go func(i int){ defer wg.Done(); objects:=make([]*node,4096); for j:=range objects { objects[j]=&node{i+j} }; runtime.GC(); results[i]=objects[2048].x }(i) }
	wg.Wait(); for i,v:=range results { if v!=i+2048 { panic("gc/closure/concurrency") } }
	fmt.Println("go-correctness-ok")
}
