#include <cmath>
#include <cstdint>
#include <iostream>

extern "C" {

// Returns a positive double (e_p2l spread) if greater than target, otherwise
// returning -1.0. Inlines the math for extreme latency optimization.
double check_edge_p2l(double pac_ask, double lig_bid) {
  if (pac_ask <= 0.0 || lig_bid <= 0.0)
    return -9999.0;
  return (lig_bid - pac_ask) / pac_ask;
}

double check_edge_l2p(double lig_ask, double pac_bid) {
  if (lig_ask <= 0.0 || pac_bid <= 0.0)
    return -9999.0;
  return (pac_bid - lig_ask) / lig_ask;
}

// Evaluates the closing condition strictly avoiding Python's floating point
// overhead. Returns 1 if condition met (should close), 0 otherwise.
int check_close_condition(double pac_bid, double pac_ask, double lig_bid,
                          double lig_ask, double close_target,
                          int is_target_long) {

  double current_close_edge = -9999.0;

  if (is_target_long == 1) {
    // Target(Pac)->Lig (Opened P2L, Close L2P)
    // Cierre: Comprar en Pac (Ask), Vender en Lig (Bid)
    if (lig_ask > 0.0 && pac_bid > 0.0) {
      current_close_edge = (pac_bid - lig_ask) / lig_ask;
    }
  } else {
    // Lig->Target (Opened L2P, Close P2L)
    if (pac_ask > 0.0 && lig_bid > 0.0) {
      current_close_edge = (lig_bid - pac_ask) / pac_ask;
    }
  }

  // Debug Print
  if (current_close_edge > 0.02 ||
      current_close_edge <
          -0.02) { // Print only significant simulated/real deviations
    std::cout << "[C++ DEBUG] P_bid:" << pac_bid << " L_ask:" << lig_ask
              << " | tgt_long:" << is_target_long
              << " | CalcEdge: " << current_close_edge
              << " | Req: " << close_target << std::endl;
  }

  if (current_close_edge != -9999.0 && current_close_edge >= close_target) {
    return 1;
  }

  return 0;
}
}
