//! Build script compiling hand-tuned x86_64 AVX2/FMA GNU Assembly kernels.

fn main() {
    println!("cargo:rerun-if-changed=asm/xps17_avx2_kernels.S");
    println!("cargo:rerun-if-changed=build.rs");

    let target_arch = std::env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default();
    if target_arch == "x86_64" {
        cc::Build::new()
            .file("asm/xps17_avx2_kernels.S")
            .flag("-O3")
            .flag("-mavx2")
            .flag("-mfma")
            .flag("-fPIC")
            .compile("xps17_avx2_kernels");
    }
}
