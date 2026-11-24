# Offline kernel safety & policy verification Makefile
# All compiles and tests execute on E-cores (CPUs 12-19) at nice 19.

CC      := gcc
CFLAGS  := -O2 -std=c11 -Wall -Wextra -Werror -Wpedantic -fsanitize=address,undefined
LDFLAGS := -fsanitize=address,undefined -lm
INCDIR  := ../rt1316-xps17
ECORE   := taskset -c 12-19 nice -n 19

TARGET  := test_rt1316_policy
SRC     := test_rt1316_policy.c
HDRS    := $(INCDIR)/rt1316-xps17-policy.h $(INCDIR)/rt1316-xps17-bq-tables.h

.PHONY: all test mock elf verify clean

all: $(TARGET)

$(TARGET): $(SRC) $(HDRS)
	$(ECORE) $(CC) $(CFLAGS) -I$(INCDIR) $(SRC) -o $(TARGET) $(LDFLAGS)

test: $(TARGET)
	$(ECORE) ./$(TARGET)

mock:
	$(ECORE) ./test_scripts_mock.sh

elf:
	$(ECORE) ./verify_ko_elf.sh

verify:
	$(ECORE) ./run_all_verify.sh

clean:
	rm -f $(TARGET)
