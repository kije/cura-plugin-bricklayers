//! Wasmtime runtime for calling the BrickLayers WASM algorithm module.

use std::path::Path;
use wasmtime::*;
use wasmtime_wasi::WasiCtxBuilder;
use wasmtime_wasi::p1::WasiP1Ctx;

use super::BrickSettings;

pub struct WasmRuntime {
    store: Store<WasiP1Ctx>,
    instance: Instance,
    memory: Memory,
}

impl WasmRuntime {
    pub fn new(wasm_path: &Path) -> Result<Self, Box<dyn std::error::Error>> {
        let engine = Engine::default();
        let module = Module::from_file(&engine, wasm_path)?;

        // Set up WASI context (the WASM module needs basic WASI imports)
        let wasi_ctx = WasiCtxBuilder::new().build_p1();
        let mut store = Store::new(&engine, wasi_ctx);

        // Link WASI imports
        let mut linker = Linker::new(&engine);
        wasmtime_wasi::p1::add_to_linker_sync(&mut linker, |ctx| ctx)?;

        let instance = linker.instantiate(&mut store, &module)?;

        let memory = instance
            .get_memory(&mut store, "memory")
            .ok_or("WASM module missing 'memory' export")?;

        Ok(Self {
            store,
            instance,
            memory,
        })
    }

    /// Push settings into the WASM module.
    pub fn set_settings(&mut self, s: &BrickSettings) {
        let func = match self.instance.get_typed_func::<(u32, i64, i64, u32, u32, i64, i64), ()>(
            &mut self.store,
            "set_settings",
        ) {
            Ok(f) => f,
            Err(e) => {
                tracing::warn!("WASM: set_settings not found: {}", e);
                return;
            }
        };

        let multiplier_x1000 = (s.extrusion_multiplier * 1000.0) as i64;

        if let Err(e) = func.call(
            &mut self.store,
            (
                s.enabled as u32,
                s.start_layer,
                s.end_layer,
                s.apply_inner_walls as u32,
                s.apply_outer_walls as u32,
                multiplier_x1000,
                s.layer_height,
            ),
        ) {
            tracing::warn!("WASM: set_settings call failed: {}", e);
        }
    }

    /// Call the WASM module to process a layer's paths.
    ///
    /// Input: serialized protobuf bytes (ModifyRequest)
    /// Output: serialized protobuf bytes (ModifyResponse)
    pub fn process_layer(&mut self, input: &[u8]) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
        // Allocate memory in WASM for input
        let alloc = self
            .instance
            .get_typed_func::<u32, u32>(&mut self.store, "alloc")?;
        let input_ptr = alloc.call(&mut self.store, input.len() as u32)?;

        // Write input bytes into WASM memory
        self.memory
            .write(&mut self.store, input_ptr as usize, input)?;

        // Allocate space for the output length (4 bytes)
        let output_len_ptr = alloc.call(&mut self.store, 4)?;

        // Call process_layer(input_ptr, input_len, output_len_ptr) -> output_ptr
        let process = self
            .instance
            .get_typed_func::<(u32, u32, u32), u32>(&mut self.store, "process_layer")?;
        let output_ptr = process.call(
            &mut self.store,
            (input_ptr, input.len() as u32, output_len_ptr),
        )?;

        if output_ptr == 0 {
            return Err("WASM process_layer returned null".into());
        }

        // Read output length
        let mut len_bytes = [0u8; 4];
        self.memory
            .read(&self.store, output_len_ptr as usize, &mut len_bytes)?;
        let output_len = u32::from_le_bytes(len_bytes) as usize;

        // Read output bytes
        let mut output = vec![0u8; output_len];
        self.memory
            .read(&self.store, output_ptr as usize, &mut output)?;

        // Free WASM allocations
        let dealloc = self
            .instance
            .get_typed_func::<(u32, u32), ()>(&mut self.store, "dealloc")?;
        let _ = dealloc.call(&mut self.store, (input_ptr, input.len() as u32));
        let _ = dealloc.call(&mut self.store, (output_ptr, output_len as u32));
        let _ = dealloc.call(&mut self.store, (output_len_ptr, 4));

        Ok(output)
    }
}
