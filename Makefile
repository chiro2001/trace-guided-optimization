PYTHON ?= python3
JOBS ?= 8

.PHONY: deps test bench appimage
deps:
	$(PYTHON) scripts/bootstrap.py --jobs $(JOBS)

test:
	$(PYTHON) -m unittest discover -s tests -v

bench:
	$(PYTHON) scripts/build_bench.py

appimage: deps
	$(PYTHON) scripts/package_appimage.py --qemu-header deps/install/include/qemu-plugin.h
