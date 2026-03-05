# Offline test for phase vocoder resynthesis (host build)
CXX = g++
CXXFLAGS         = -std=c++14 -O2 -Wall -I.. -I../DaisySP/Source -I../DaisySP/Source/Utility
TARGET           = test_resynth_offline
TARGET_CV        = test_resynth_cv_sweeps
TARGET_FLUFF     = test_resynth_fluff
TARGET_HARMONIC  = test_resynth_harmonic
TARGET_PROPS     = test_resynth_props
TARGET_VOCT      = test_resynth_voct
SRCS             = test_resynth_offline.cpp wav_io.cpp
SRCS_CV          = test_resynth_cv_sweeps.cpp wav_io.cpp
SRCS_FLUFF       = test_resynth_fluff.cpp wav_io.cpp
SRCS_HARMONIC    = test_resynth_harmonic.cpp wav_io.cpp
SRCS_PROPS       = test_resynth_props.cpp
SRCS_VOCT        = test_resynth_voct.cpp wav_io.cpp

.PHONY: all run cv_sweeps fluff harmonic tests samples props voct

all: $(TARGET) $(TARGET_CV) $(TARGET_FLUFF) $(TARGET_HARMONIC) $(TARGET_PROPS) $(TARGET_VOCT)

$(TARGET): $(SRCS)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SRCS)

$(TARGET_CV): $(SRCS_CV)
	$(CXX) $(CXXFLAGS) -o $(TARGET_CV) $(SRCS_CV)

$(TARGET_FLUFF): $(SRCS_FLUFF)
	$(CXX) $(CXXFLAGS) -o $(TARGET_FLUFF) $(SRCS_FLUFF)

$(TARGET_HARMONIC): $(SRCS_HARMONIC)
	$(CXX) $(CXXFLAGS) -o $(TARGET_HARMONIC) $(SRCS_HARMONIC)

$(TARGET_PROPS): $(SRCS_PROPS)
	$(CXX) $(CXXFLAGS) -o $(TARGET_PROPS) $(SRCS_PROPS)

$(TARGET_VOCT): $(SRCS_VOCT)
	$(CXX) $(CXXFLAGS) -o $(TARGET_VOCT) $(SRCS_VOCT) \
		../DaisySP/Source/Filters/svf.cpp \
		../DaisySP/Source/Control/phasor.cpp

# V/OCT sweep: all WAVs in samples/ -> out/voct_sweep/{basename}_voct_sweep.wav
run: $(TARGET)
	./$(TARGET)

# CV sweeps: all WAVs in samples/ -> out/cv_sweep/ (no MAX COMP) and out/cv_sweep_maxcomp/ (MAX COMP)
cv_sweeps: $(TARGET_CV)
	./$(TARGET_CV)

# FLUFF scale test: all WAVs in samples/ -> out/fluff/{basename}_fluff_combo{0..4}.wav
fluff: $(TARGET_FLUFF)
	./$(TARGET_FLUFF)

# Harmonic-stack simplified algorithm tests:
#   out/harmonic/{basename}_harmonic_C2.wav
#   out/harmonic/{basename}_harmonic_voct_sweep.wav
harmonic: $(TARGET_HARMONIC)
	./$(TARGET_HARMONIC)

# Property / stability tests: fast checks that avoid generating
# subjective audio renders. These are intended for automation.
props: $(TARGET_PROPS)
	./$(TARGET_PROPS)

# V/OCT harmonic analysis: OneShotOneOsc.wav (or arg) -> out/voct_harmonic/*.csv, *.svg
voct: $(TARGET_VOCT)
	./$(TARGET_VOCT)

# Sample render suite: V/OCT sweep, CV sweeps, FLUFF, harmonic stack.
# These generate WAVs under test/out/ for subjective listening.
samples: run cv_sweeps fluff harmonic

# Back-compat: "make tests" in this directory runs the fast
# property checks without emitting WAVs.
tests: props

clean:
	rm -f $(TARGET) $(TARGET_CV) $(TARGET_FLUFF) $(TARGET_HARMONIC) $(TARGET_PROPS) $(TARGET_VOCT)
