### [Go Back](./index.md#table-of-contents)

# Yosys Memory Mapping

OpenRAM can write a Yosys `memory_libmap` description and a Verilog technology
map for a generated SRAM. These let Yosys infer the SRAM from a behavioral
memory instead of implementing it with registers.

## Generate the Mapping Files

Add this option to your SRAM configuration:

```python
output_yosys = True
```

For `output_name = "my_sram"`, the compiler writes `my_sram.memlib` and
`my_sram_map.v` alongside the existing output files. The option is off by
default. You can also write the files through the Python library:

```python
s.yosys_write("my_sram.memlib", "my_sram_map.v")
```

Here, `s` is an SRAM created with `openram.sram`.

The mapping supports single-bank SRAMs with a power-of-two depth and no spare
rows. Spare columns are left unused with their write enables tied low. Read,
write and combined read/write ports use the generated macro's port numbering.

Each mapped macro uses full-word writes. Yosys can split wider memories or
memories with partial write enables across multiple macros. This can use more
macros than a mapping that uses the native write masks. The generated cost is
an estimate in 64-bit units; adjust it when comparing macros with different
area costs.

## Infer an SRAM

For a single read/write port, a memory can be described as follows:

```verilog
(* ram_style = "block" *) reg [7:0] mem [0:15];

always @(posedge clk) begin
    if (enable) begin
        if (write_enable)
            mem[address] <= data_in;
        else
            data_out <= mem[address];
    end
end
```

The mapping does not promise a read result during a write or initial memory
contents. Yosys checks these requirements before selecting the SRAM. For
separate ports, simultaneous reads and writes to the same address have
undefined results. Only use Yosys's `no_rw_check` attribute if the design avoids
these collisions or allows undefined results.

OpenRAM captures inputs on the rising clock edge and completes reads in the
second half of the cycle. In simulation, allow the generated model's `DELAY`
after the falling edge before checking read data. Use the generated Liberty
files for implementation timing.

## Run Yosys

The Nix development environment includes Yosys. You can also use an existing
Yosys installation with the `memory_libmap` pass.

Run the following from the directory containing the generated files and your
RTL. Replace `top` with your design's top module.

```yosys
read_verilog -lib my_sram.v
read_verilog -sv design.v
hierarchy -top top
proc
opt
memory -nomap
memory_libmap -lib my_sram.memlib
techmap -map my_sram_map.v
memory_map
opt
check -assert
stat
write_verilog -noattr mapped.v
```

Continue with your usual standard-cell mapping steps. The generated SRAM is
read as a black box for synthesis. Compile its original Verilog model together
with `mapped.v` for simulation.

The [Yosys memory library documentation](https://github.com/YosysHQ/yosys/blob/main/passes/memory/memlib.md)
describes the library format and mapping rules.
