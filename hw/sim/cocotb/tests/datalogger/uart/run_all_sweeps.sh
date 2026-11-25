#!/bin/bash
# rr unknown-llm (AI-generated)

# Script to run split UART datalogger sweeps
# Logs are captured to individual files

mkdir -p logs/sweeps

echo "Starting UART Datalogger Sweep Suite (Baud >= 115200)..."

# 1. Smoke test
echo "Running Smoke Test (115200, 8N1)..."
SIM=nvc python test_uart_datalogger_smoke.py 2>&1 | tee logs/sweeps/smoke.log
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo "❌ Smoke Test Failed! Aborting further sweeps."
    exit 1
fi
echo "✅ Smoke Test Passed."

# 2. Format Sweep (fixed baud 1M, sweep bits/parity)
echo "Running Format Sweep (Bits 5-9, Parity, 1M)..."
SIM=nvc python test_uart_datalogger_format_sweep.py 2>&1 | tee logs/sweeps/sweep_format_1M.log
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo "⚠️  Format Sweep had some failures. See logs/sweeps/sweep_format_1M.log"
else
    echo "✅ Format Sweep Passed."
fi

# 3. Baud Rate Sweep (fixed 8N1, sweep bauds)
echo "Running Baud Rate Sweep (115200 - 3M)..."
SIM=nvc python test_uart_datalogger_baud_sweep.py 2>&1 | tee logs/sweeps/sweep_baud.log
if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo "⚠️  Baud Rate Sweep had some failures. See logs/sweeps/sweep_baud.log"
else
    echo "✅ Baud Rate Sweep Passed."
fi

echo "Sweep Suite Finished."
