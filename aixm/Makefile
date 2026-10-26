# Build, cross-check and validate the HUEN AIXM 5.1.1 data set.
#
#   make            build -> crosscheck -> validate
#   make build      regenerate out/HUEN_AIP_DS.xml from data/huen.yaml
#   make validate   schema + domain checks, writes out/HUEN_validation_report.md
#   make clean      remove generated output
#
# `make validate` exits non-zero if the schema fails or any ERROR is raised,
# so it is usable as a CI gate.

PYTHON ?= python3

.PHONY: all build crosscheck validate clean

all: build crosscheck validate

build:
	$(PYTHON) scripts/build_aixm.py

crosscheck:
	$(PYTHON) scripts/track_crosscheck.py

validate: 
	$(PYTHON) scripts/validate_aixm.py

clean:
	rm -f out/HUEN_AIP_DS.xml out/build_manifest.json \
	      out/track_crosscheck.json out/HUEN_validation_report.md
