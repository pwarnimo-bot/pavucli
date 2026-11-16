NAME     := pavucli
VERSION  := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' pavucli/__init__.py)
PREFIX   ?= /usr/local
DESTDIR  ?=
PYTHON   ?= python3
TARBALL  := dist/$(NAME)-$(VERSION).tar.gz
SRC_FILES := pavucli tests man data pyproject.toml README.md LICENSE Makefile packaging debian

.PHONY: all run test dist install uninstall arch rpm deb clean

all: test

run:
	$(PYTHON) -m pavucli

test:
	$(PYTHON) -m unittest discover -s tests -v

# Source tarball used by the distro packages
dist: $(TARBALL)
$(TARBALL): $(shell find pavucli -name '*.py') pyproject.toml
	mkdir -p dist
	tar --exclude='__pycache__' --exclude='packaging/arch/src' --exclude='packaging/arch/pkg' \
	    --exclude='*.pkg.tar.*' --exclude='packaging/arch/*.tar.gz' \
	    --transform 's,^,$(NAME)-$(VERSION)/,' -czf $@ $(SRC_FILES)
	@echo "created $@"

# Plain install without a package manager (needs python-build + python-installer)
install:
	rm -rf build/wheel
	$(PYTHON) -m build --wheel --no-isolation --outdir build/wheel
	$(PYTHON) -m installer --destdir="$(DESTDIR)" --prefix="$(PREFIX)" build/wheel/*.whl
	install -Dm644 man/pavucli.1 "$(DESTDIR)$(PREFIX)/share/man/man1/pavucli.1"
	install -Dm644 data/pavucli.desktop "$(DESTDIR)$(PREFIX)/share/applications/pavucli.desktop"

uninstall:
	@echo "Remove $(PREFIX)/bin/pavucli, the pavucli package from site-packages,"
	@echo "$(PREFIX)/share/man/man1/pavucli.1 and $(PREFIX)/share/applications/pavucli.desktop"

# Arch Linux: builds packaging/arch/pavucli-*.pkg.tar.zst
arch: dist
	cp $(TARBALL) packaging/arch/
	cd packaging/arch && makepkg -f --cleanbuild

# Fedora / openSUSE: needs rpm-build and pyproject-rpm-macros
rpm: dist
	rpmbuild -bb --define "_sourcedir $(CURDIR)/dist" --define "_rpmdir $(CURDIR)/dist" \
	    packaging/rpm/pavucli.spec

# Debian / Ubuntu: needs devscripts, debhelper, dh-python, pybuild-plugin-pyproject
deb:
	dpkg-buildpackage -us -uc -b
	mkdir -p dist && mv ../$(NAME)_$(VERSION)*_all.deb dist/

clean:
	rm -rf build dist *.egg-info pavucli.egg-info .pybuild
	rm -rf packaging/arch/src packaging/arch/pkg packaging/arch/*.tar.gz packaging/arch/*.pkg.tar.*
	find . -name __pycache__ -prune -exec rm -rf {} +
