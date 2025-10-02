#!/usr/bin/env python3
"""Quick burned-register helper
 
Usage:
  python burn_register_miner.py --wallet.name WALLET --wallet.hotkey HOTKEY \
      --netuid 4 --network finney
 
The script:
  • Unlocks the coldkey password once.
  • Shows the current burn price (SubtensorModule.Burn(netuid)).
  • Issues a burnedRegister extrinsic and waits for finalization.
If the extrinsic succeeds it prints the new UID; otherwise prints the
module error key.
"""
import argparse, getpass, sys, time, logging, traceback
import bittensor as bt
import datetime


def parse_args():
    p = argparse.ArgumentParser(description="Instant burnedRegister for a miner hotkey (defaults shown)")
    p.add_argument("--wallet.name", default="default", help="Coldkey name (default: 'default')")
    p.add_argument("--hotkeys", default="hk0", help="Comma-separated hotkey names to try each block")
    p.add_argument("--netuid", type=int, default=43, help="Subnet UID (default: 34)")
    p.add_argument("--network", default="finney", help="Subtensor chain endpoint (default: finney)")
    p.add_argument("--tip", type=float, default=0.0, help="Optional tip in TAO (default: 0)")
    p.add_argument("--log_file", default="burn_register_miner.log", help="Path to logfile (default: burn_register_miner.log)")
    p.add_argument("--pre", type=float, default=1.0275, help="Seconds before block boundary to send tx (latency lead)")
    p.add_argument("--retry", type=int, default=48, help="Seconds after window start to keep retrying (default 48)")
    p.add_argument("--last_reg_time_utc", default="2025-09-09 03:01:36", help="UTC timestamp of last successful registration (YYYY-MM-DD HH:MM:SS)")
    return p.parse_args()
 
 
# ------------- logging helpers -------------
logger = logging.getLogger("burn_register")
 
 
def setup_logging(path: str):
    logger.setLevel(logging.INFO)
    class MicrosecondFormatter(logging.Formatter):
        """Custom formatter that supports %f (microseconds) in datefmt on all Python versions."""
 
        def formatTime(self, record, datefmt=None):
            dt = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).astimezone()
            if datefmt:
                return dt.strftime(datefmt)
            # Fallback to default implementation if no datefmt provided
            return super().formatTime(record, datefmt)
 
    fmt = MicrosecondFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S.%f")
 
    fh = logging.FileHandler(path)
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
 
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
 
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    return fh  # for reuse with bittensor logger
 
 
def log(msg: str, level: str = "info"):
    getattr(logger, level)(msg)
 
 
def main():
    args = parse_args()
 
    # initialise logging
    fh = setup_logging(args.log_file)
    log(f"Logging initialized. Writing to {args.log_file}")
 
    # Map argparse names to easier ones
    wallet_name = getattr(args, "wallet.name")
    hotkey_names = [hk.strip() for hk in getattr(args, "hotkeys").split(",")]
 
    # Wallet set-up for all hotkeys-----------------------------------------
    wallets = []
    try:
        for hk in hotkey_names:
            w = bt.wallet(name=wallet_name, hotkey=hk)
            w.unlock_coldkey()
            wallets.append(w)
    except Exception as e:
        log(f"Failed to decrypt coldkey: {e}", "error")
        sys.exit(1)
 
    # Connect --------------------------------------------------------------
    # Attach bittensor’s internal logger (it uses the root logger under the hood)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(fh)
 
    # Main connection for informational calls
    sub = bt.Subtensor(network=args.network)
    log(f"Connected to {args.network} – current block {sub.block}")
 
    # Pre-create one dedicated connection per wallet so that the costly
    # WebSocket handshake is done **before** we enter the registration race.
    subs_for_wallets = []
    for _ in wallets:
        s = bt.Subtensor(network=args.network)
        # Touch the connection once to finish the handshake
        try:
            _ = s.get_current_block()
        except Exception:
            pass
        subs_for_wallets.append(s)
 
    CYCLE_BLOCKS = 360
    BLOCK_TIME_S = 12
 
    def current_burn_cost():
        try:
            return sub.burn(args.netuid)
        except Exception:
            return None
 
    def attempt_with_wallet(w: bt.wallet, local_sub: bt.Subtensor) -> bool:
        """Attempt registration with an isolated Subtensor connection.
        Each thread maintains its own WebSocket to avoid concurrency errors inside
        async_substrate_interface ("cannot call recv while another thread is already running").
        """
        try:
            log(f"Sending burnedRegister from hotkey {w.hotkey.ss58_address[:6]}…")
            return local_sub.burned_register(
                wallet=w,
                netuid=args.netuid,
                wait_for_inclusion=True,
                wait_for_finalization=False
            )
        except Exception:
            log(f"Exception in hotkey {w.hotkey.ss58_address[:6]}…: {traceback.format_exc()}", "warning")
            return False
 
    success = False
 
    while not success:
        # calculate next target datetime based on last_reg_time_utc
        last_dt = datetime.datetime.strptime(args.last_reg_time_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        cycle_seconds = CYCLE_BLOCKS * BLOCK_TIME_S
        target_dt = last_dt
        now_dt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
        while target_dt <= now_dt:
            target_dt += datetime.timedelta(seconds=cycle_seconds)
 
        start_dt = target_dt - datetime.timedelta(seconds=args.pre)
        sleep = (start_dt - now_dt).total_seconds()
        log(f"Next window starts {target_dt.strftime('%H:%M:%S.%f')} UTC – sleeping {sleep:.3f}s to pre-time …")
        if sleep > 0:
            time.sleep(sleep)
 
        # Log burn cost once at the beginning of this registration window
        cost_window = current_burn_cost()
        if cost_window is not None:
            log(f"Burn cost for this window: {cost_window.tao} TAO")
 
        # Schedule exactly one registration attempt per block using different hotkeys.
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        executor = ThreadPoolExecutor(max_workers=len(wallets))
        futures = []
 
        for idx, w in enumerate(wallets):
            # Each wallet is assigned to a consecutive block: 0, 1, 2 …
            attempt_dt = target_dt + datetime.timedelta(seconds=idx * BLOCK_TIME_S)
            launch_dt  = attempt_dt - datetime.timedelta(seconds=args.pre)
 
            # Sleep until pre-launch moment for this wallet
            now_dt_loop = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
            sleep_secs  = (launch_dt - now_dt_loop).total_seconds()
            if sleep_secs > 0:
                log(f"Sleeping {sleep_secs:.3f}s before launching hotkey {w.hotkey.ss58_address[:6]}… for block {attempt_dt.strftime('%H:%M:%S.%f')}")
                time.sleep(sleep_secs)
 
            log(f"Launching burnedRegister for hotkey {w.hotkey.ss58_address[:6]}… targeting block {attempt_dt.strftime('%H:%M:%S.%f')}")
            futures.append(executor.submit(attempt_with_wallet, w, subs_for_wallets[idx]))
 
        # Wait until the first future succeeds (or all finish without success)
        while futures and not success:
            done, pending = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    if fut.result():
                        success = True
                        break
                except Exception:
                    log(f"Unhandled future exception: {traceback.format_exc()}", "warning")
            futures = list(pending)
 
        # Clean up executor
        executor.shutdown(wait=False)
 
        # If none of the three attempts succeeded, move on to next window
        if not success:
            args.last_reg_time_utc = target_dt.strftime("%Y-%m-%d %H:%M:%S")
            log("Window complete without successful registration, preparing for next window…")
 
    if success:
        log("✅ burnedRegister succeeded!", "info")
        for w in wallets:
            try:
                uid = sub.get_uid_for_hotkey_on_subnet(w.hotkey.ss58_address, args.netuid)
                if uid is not None:
                    log(f"Hotkey {w.hotkey.ss58_address[:6]}… got UID {uid}")
            except Exception:
                pass
    else:
        log("Registration failed after all attempts.", "error")
        sys.exit(1)
 
 
if __name__ == "__main__":
    main()
