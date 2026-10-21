#!/usr/bin/env python3
# See LICENSE for licensing information.
#
# Copyright (c) 2016-2026 Regents of the University of California, Santa Cruz
# All rights reserved.
#
"""Map behavioral memories to generated SRAMs and simulate the mapped design."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import openram
from openram import OPTS
from testutils import debugTestRunner, header, openram_test


mapping_tools = all(shutil.which(tool) for tool in ["yosys", "iverilog", "vvp"])


class yosys_sram_test(openram_test):

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def make_sram(self, ports=(1, 0, 0), write_size=None, spare_cols=None):
        OPTS.num_rw_ports, OPTS.num_w_ports, OPTS.num_r_ports = ports
        OPTS.word_size = 8
        OPTS.num_words = 16
        OPTS.write_size = write_size
        OPTS.netlist_only = True
        OPTS.check_lvsdrc = False
        config = os.path.join(os.environ["OPENRAM_HOME"], "tests/configs/config")
        openram.init_openram(config, is_unit_test=True)
        self.addCleanup(openram.end_openram)
        if spare_cols is None:
            spare_cols = int(OPTS.tech_name == "sky130" and sum(ports) == 1)
        config = openram.sram_config(word_size=8, num_words=16,
                                    words_per_row=1, write_size=write_size,
                                    num_spare_cols=spare_cols)
        return openram.sram(config, "yosys_test")

    def run_tool(self, arguments, success=True):
        result = subprocess.run(arguments, cwd=self.directory, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True,
                                timeout=60)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def behavioral_memory(self, sram, width, masked, depth=16):
        ports = ["input clk"]
        operations = []
        address_width = (depth - 1).bit_length()
        for port in sram.all_ports:
            ports.extend(["input en{}".format(port),
                          "input [{}:0] addr{}".format(address_width - 1, port)])
            operation = "  always @(posedge clk) if (en{}) begin\n".format(port)
            if port in sram.write_ports:
                ports.extend(["input we{}".format(port),
                              "input [{}:0] din{}".format(width - 1, port)])
                operation += "    if (we{}) begin\n".format(port)
                if masked:
                    ports.append("input [{}:0] mask{}".format(width - 1, port))
                    operation += ("      for (integer bit_index = 0; bit_index < {0}; bit_index = bit_index + 1)\n"
                                  "        if (mask{1}[bit_index]) mem[addr{1}][bit_index] <= din{1}[bit_index];\n").format(width, port)
                else:
                    operation += "      mem[addr{0}] <= din{0};\n".format(port)
                operation += "    end\n"
            if port in sram.read_ports:
                ports.append("output reg [{}:0] dout{}".format(width - 1, port))
                if port in sram.write_ports:
                    operation += "    else "
                else:
                    operation += "    "
                operation += "dout{0} <= mem[addr{0}];\n".format(port)
            operation += "  end\n"
            operations.append(operation)
        # Simultaneous accesses through different ports must use distinct addresses.
        return ("module top({});\n"
                '  (* ram_style = "block", no_rw_check *) reg [{}:0] mem[0:{}];\n'
                "{}endmodule\n").format(", ".join(ports), width - 1, depth - 1, "".join(operations))

    def make_testbench(self, sram, width, masked, depth=16):
        declarations = ["reg clk = 0;", "always #10 clk = ~clk;",
                        "reg [{}:0] expected_mem[0:{}];".format(width - 1, depth - 1), "integer cycle;"]
        connections = [".clk(clk)"]
        initialize = []
        stimulate = []
        update = []
        check = []
        first_write = sram.write_ports[0]
        address_width = (depth - 1).bit_length()
        for port in sram.all_ports:
            declarations.extend(["reg en{} = 0;".format(port),
                                 "reg [{}:0] addr{} = 0;".format(address_width - 1, port)])
            connections.extend([".en{0}(en{0})".format(port), ".addr{0}(addr{0})".format(port)])
            initialize.extend(["en{0} = {1};".format(port, int(port == first_write)),
                               "addr{} = cycle;".format(port)])
            stimulate.extend(["en{0} = ((cycle + {0}) % 7 != 0);".format(port),
                              "addr{0} = (cycle * 3 + {0} * 5) % {1};".format(port, depth)])
            if port in sram.write_ports:
                declarations.extend(["reg we{} = 0;".format(port),
                                     "reg [{}:0] din{} = 0;".format(width - 1, port)])
                connections.extend([".we{0}(we{0})".format(port), ".din{0}(din{0})".format(port)])
                initialize.extend(["we{} = 1;".format(port), "din{} = cycle * 17;".format(port)])
                stimulate.extend(["we{0} = ((cycle + {0}) % 4 == 0);".format(port),
                                  "din{0} = (cycle + {0}) * 32'h9e3779b9;".format(port)])
                if masked:
                    declarations.append("reg [{}:0] mask{} = 0;".format(width - 1, port))
                    connections.append(".mask{0}(mask{0})".format(port))
                    initialize.append("mask{} = '1;".format(port))
                    stimulate.append("mask{0} = (cycle % 3 == 0) ? '0 : (cycle % 3 == 1) ? '1 : (cycle + {0}) * 32'h6a09e667;".format(port))
                    update.append(("if (en{0} && we{0})\n"
                                   "      expected_mem[addr{0}] = (expected_mem[addr{0}] & ~mask{0}) | (din{0} & mask{0});").format(port))
                else:
                    update.append("if (en{0} && we{0}) expected_mem[addr{0}] = din{0};".format(port))
            if port in sram.read_ports:
                declarations.extend(["wire [{}:0] dout{};".format(width - 1, port),
                                     "reg [{}:0] expected{} = 'x;".format(width - 1, port)])
                connections.append(".dout{0}(dout{0})".format(port))
                enable = "en{}".format(port)
                if port in sram.write_ports:
                    enable += " && !we{}".format(port)
                update.append("if ({0}) expected{1} = expected_mem[addr{1}];".format(enable, port))
                check.append(('if (dout{0} !== expected{0}) '
                              '$fatal(1, "port {0} cycle %0d got %h expected %h", cycle, dout{0}, expected{0});').format(port))
        return ("`timescale 1ns/1ps\nmodule tb;\n  {declarations}\n"
                "  top dut({connections});\n  initial begin\n"
                "    for (cycle = 0; cycle < {depth}; cycle = cycle + 1) begin\n"
                "      {initialize}\n      expected_mem[cycle] = din{first_write};\n"
                "      @(negedge clk); #5;\n    end\n"
                "    for (cycle = 0; cycle < 128; cycle = cycle + 1) begin\n"
                "      {stimulate}\n      {update}\n"
                "      @(negedge clk); #4;\n      {check}\n      #1;\n    end\n"
                '    $display("MAPPING PASSED");\n    $finish;\n  end\nendmodule\n').format(
                    declarations="\n  ".join(declarations), connections=", ".join(connections),
                    initialize="\n      ".join(initialize), first_write=first_write,
                    stimulate="\n      ".join(stimulate), update="\n      ".join(update),
                    check="\n      ".join(check), depth=depth)

    def create_mapping(self, ports=(1, 0, 0), write_size=None, spare_cols=None):
        sram = self.make_sram(ports, write_size, spare_cols)
        sram.verilog_write(str(self.directory / "macro.v"))
        sram.yosys_write(str(self.directory / "macro.memlib"), str(self.directory / "map.v"))
        return sram

    def check_mapping(self, ports=(1, 0, 0), width=8, masked=False, write_size=None, spare_cols=None, depth=16):
        sram = self.create_mapping(ports, write_size, spare_cols)
        (self.directory / "design.v").write_text(self.behavioral_memory(sram.s, width, masked, depth))
        (self.directory / "tb.v").write_text(self.make_testbench(sram.s, width, masked, depth))
        self.synthesize_and_simulate(sram, width, write_size, depth)

    def synthesize_and_simulate(self, sram, width=8, write_size=None, depth=16):
        script = ("read_verilog -lib macro.v; read_verilog -sv design.v; hierarchy -top top; "
                  "proc; opt; memory -nomap; memory_libmap -lib macro.memlib; "
                  "techmap -map map.v; opt; check -assert; write_json mapped.json; "
                  "write_verilog -noattr mapped.v")
        self.run_tool(["yosys", "-Q", "-T", "-p", script])
        cells = json.loads((self.directory / "mapped.json").read_text())["modules"]["top"]["cells"]
        macros = [cell for cell in cells.values() if cell["type"] == sram.s.name]
        self.assertGreaterEqual(len(macros), ((width + 7) // 8) * ((depth + 15) // 16))
        self.assertFalse(any(cell["type"].startswith("$mem") for cell in cells.values()))
        if write_size:
            for macro in macros:
                for port in sram.s.write_ports:
                    self.assertEqual(macro["connections"]["wmask{}".format(port)], ["1", "1"])
        if sram.s.num_spare_cols:
            for macro in macros:
                for port in sram.s.write_ports:
                    self.assertEqual(macro["connections"]["spare_wen{}".format(port)], ["0"] * sram.s.num_spare_cols)
        self.run_tool(["iverilog", "-g2012", "-s", "tb", "-o", "simulation",
                       "tb.v", "macro.v", "mapped.v"])
        self.assertIn("MAPPING PASSED", self.run_tool(["vvp", "simulation"]))

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_single_read_write_port(self):
        self.check_mapping()

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_memory_wider_than_macro(self):
        self.check_mapping(width=22)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_memory_wider_and_deeper_than_macro(self):
        self.check_mapping(width=22, depth=128)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_partial_writes_with_masked_macro(self):
        self.check_mapping(width=22, masked=True, write_size=4)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_separate_read_and_write_ports(self):
        self.check_mapping(ports=(0, 1, 1))

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_read_write_and_read_only_ports(self):
        self.check_mapping(ports=(1, 0, 1))

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_two_read_write_ports(self):
        self.check_mapping(ports=(2, 0, 0))

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_unused_spare_columns(self):
        self.check_mapping(spare_cols=3)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_independent_port_clocks(self):
        sram = self.create_mapping(ports=(0, 1, 1))
        source = self.behavioral_memory(sram.s, 8, False)
        source = source.replace("input clk,", "input clk0, input clk1,")
        for port in sram.s.all_ports:
            source = source.replace("@(posedge clk) if (en{})".format(port),
                                    "@(posedge clk{}) if (en{})".format(port, port))
        (self.directory / "design.v").write_text(source)
        (self.directory / "tb.v").write_text("""`timescale 1ns/1ps
module tb;
  reg clk0 = 0, clk1 = 0;
  always #10 clk0 = ~clk0;
  always #13 clk1 = ~clk1;
  reg en0 = 1, en1 = 0, we0 = 1;
  reg [3:0] addr0 = 0, addr1 = 0;
  reg [7:0] din0 = 0;
  wire [7:0] dout1;
  integer word;
  top dut(clk0, clk1, en0, addr0, we0, din0, en1, addr1, dout1);
  initial begin
    for (word = 0; word < 16; word = word + 1) begin
      addr0 = word;
      din0 = word * 17;
      @(negedge clk0); #5;
    end
    we0 = 0;
    for (word = 0; word < 16; word = word + 1) begin
      @(negedge clk1); #5;
      en1 = 1;
      addr1 = word;
      @(negedge clk1); #4;
      if (dout1 !== word * 17) $fatal(1, "independent clock read failed at %0d", word);
    end
    $display("MAPPING PASSED");
    $finish;
  end
endmodule
""")
        self.synthesize_and_simulate(sram)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_read_before_write_requirement_is_rejected(self):
        sram = self.create_mapping()
        source = self.behavioral_memory(sram.s, 8, False)
        source = source.replace(", no_rw_check", "").replace("else dout0", "dout0")
        (self.directory / "design.v").write_text(source)
        output = self.run_tool(["yosys", "-Q", "-T", "-p",
                               "read_verilog -sv design.v; proc; opt; memory -nomap; "
                               "memory_libmap -lib macro.memlib"], success=False)
        self.assertIn("no valid mapping found", output)

    @unittest.skipUnless(mapping_tools, "Requires Yosys and Icarus Verilog")
    def test_initialized_memory_is_rejected(self):
        sram = self.create_mapping()
        source = self.behavioral_memory(sram.s, 8, False)
        source = source.replace("endmodule", "initial for (integer i = 0; i < 16; i = i + 1) mem[i] = 0;\nendmodule")
        (self.directory / "design.v").write_text(source)
        output = self.run_tool(["yosys", "-Q", "-T", "-p",
                               "read_verilog -sv design.v; proc; opt; memory -nomap; "
                               "memory_libmap -lib macro.memlib"], success=False)
        self.assertIn("no valid mapping found", output)

    def test_unsupported_configurations_do_not_create_files(self):
        sram = self.make_sram()
        for attribute, value in [("num_banks", 2), ("num_spare_rows", 1),
                                 ("num_words", 24)]:
            with self.subTest(attribute=attribute):
                previous = getattr(sram.s, attribute)
                setattr(sram.s, attribute, value)
                with self.assertRaises(ValueError):
                    sram.yosys_write(str(self.directory / "macro.memlib"), str(self.directory / "map.v"))
                setattr(sram.s, attribute, previous)
                self.assertFalse((self.directory / "macro.memlib").exists())
                self.assertFalse((self.directory / "map.v").exists())


if __name__ == "__main__":
    (OPTS, args) = openram.parse_args()
    del sys.argv[1:]
    header(__file__, OPTS.tech_name)
    unittest.main(testRunner=debugTestRunner())
