CXX=g++
CXXFLAGS=-O3 -march=native -ffast-math -fPIC -shared -Wall

all: hft_core.so

hft_core.so: hft_core.cpp
	$(CXX) $(CXXFLAGS) hft_core.cpp -o hft_core.so

clean:
	rm -f hft_core.so
