// RUN: not zamacompiler --action=roundtrip %s  2>&1| FileCheck %s

// CHECK-LABEL: eint support only precision in ]0;7]
func @test(%arg0: !HLFHE.eint<8>) {
  return
}
