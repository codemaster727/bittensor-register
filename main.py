# example_register_via_local_node.py
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import bittensor as bt
from dotenv import load_dotenv

load_dotenv()

# CONFIG
NET = "test"                  # or 'finney' for mainnet
RPC_ENDPOINT = os.getenv("RPC_ENDPOINT")  # local lite node rpc/ws
NETUID = 1                    # target subnet id
COLDKEY_NAMES = [f"cold_{i}" for i in range(1, 11)]  # names in your keystore or mnemonics
BROADCAST_TIMEOUT = 3.0       # seconds (your "3 second" race window)
MAX_WORKERS = 8               # cap on parallel submissions
USE_EPOCH_START = True        # race on first block of the next epoch
BLOCK_TIME = 12.0             # seconds per block (approx)
TIP = 0.01                    # tip to raise priority
MAX_ALLOWED_ATTEMPTS = 3     # max attempts to register

def load_wallet(name):
    # This assumes you have wallet keystores already available to the SDK by name.
    # If you use mnemonics, load from env and create wallet programmatically instead.
    # Optionally, you can pass hotkey=name if you keep one hotkey per wallet name.
    try:
        w = bt.wallet(name=name)
    except Exception:
        # Fallback to hotkey=name if required by local keystore layout
        w = bt.wallet(name=name, hotkey=name)
    return w


def _get_registration_cost(subtensor, netuid):
    # Confirmed SDK method for burn/registration cost
    return subtensor.get_subnet_burn_cost(netuid)


def _get_wallet_balance(subtensor, wallet):
    # Confirmed SDK method via subtensor using coldkeypub address
    try:
        address = wallet.coldkeypub.ss58_address
    except Exception:
        return None
    try:
        return subtensor.get_balance(address)
    except Exception:
        return None


def _compare_balances(a, b):
    # Attempt direct comparison (bittensor.Balance supports comparisons)
    try:
        return a < b
    except Exception:
        pass
    # Convert to float if possible
    def to_float(x):
        try:
            if hasattr(x, "tao"):
                return float(x.tao)
        except Exception:
            pass
        try:
            return float(str(x).split()[0])
        except Exception:
            return 0.0
    return to_float(a) < to_float(b)


def _try_register(subtensor, wallet, netuid):
    # Use confirmed fastest path with minimal arguments
    try:
        res = subtensor.burned_register(
          wallet=wallet,
          netuid=netuid,
          wait_for_finalization=False,
          max_allowed_attempts=MAX_ALLOWED_ATTEMPTS,
          tip=TIP
        )
        return True, res
    except Exception as e:
        return False, str(e)


def _get_uid_for_hotkey(subtensor, hot_ss58, netuid):
    try:
        return subtensor.get_uid_for_hotkey_on_subnet(hot_ss58, netuid)
    except Exception:
        try:
            ok = subtensor.is_hotkey_registered_on_subnet(hot_ss58, netuid)
            return 1 if ok else None
        except Exception:
            return None


def _get_current_block(subtensor):
    # Use a concrete API name; if unavailable, return None
    try:
        return subtensor.get_current_block()
    except Exception:
        return None


def _wait_for_next_epoch_start(subtensor):
    # Poll block height and wait until the first block of the next epoch
    block = _get_current_block(subtensor)
    metagraph = subtensor.metagraph(netuid=NETUID)
    epoch_length = metagraph.tempo
    
    if block is None:
        return  # cannot sync to epoch, fall back to immediate start
    remainder = block % epoch_length
    blocks_until = (epoch_length - remainder) % epoch_length
    target_block = block + blocks_until
    if blocks_until == 0:
        # Already at epoch boundary, trigger immediately
        return
    # Coarse wait until within one block of target
    while True:
        now_block = _get_current_block(subtensor)
        if now_block is None:
            break
        if now_block >= target_block - 1:
            break
        time.sleep(2.0)
    # Fine-grained polling close to boundary
    while True:
        now_block = _get_current_block(subtensor)
        if now_block is None:
            break
        if now_block >= target_block:
            break
        time.sleep(0.01)

def main():
    print(RPC_ENDPOINT)
    subtensor = bt.subtensor(network=NET, endpoint=RPC_ENDPOINT)
    # fetch burn cost once (robust across SDK versions)
    burn_cost = _get_registration_cost(subtensor, NETUID)
    print("Burn cost:", burn_cost)
    
    # Optionally sync to next epoch boundary for best chance
    if USE_EPOCH_START:
        print("Waiting for next epoch boundary to start submissions...")
        # EPOCH_LENGTH = subtensor.blocks_per_epoch(netuid=NETUID)
        _wait_for_next_epoch_start(subtensor)

    wallets = []
    for n in COLDKEY_NAMES:
        try:
            w = load_wallet(n)
            # Ensure we have a hotkey loaded; many SDK ops require it
            try:
                _ = w.hotkey.ss58_address
            except Exception:
                # Try loading with hotkey=name as fallback
                try:
                    w = bt.wallet(name=n, hotkey=n)
                except Exception:
                    pass
            wallets.append(w)
        except Exception as e:
            print("Could not load wallet", n, e)

    if not wallets:
        print("No wallets loaded. Exiting.")
        return

    start_time = time.time()
    end_time = start_time + BROADCAST_TIMEOUT

    submitted = []
    submitted_lock = threading.Lock()
    success = None
    success_lock = threading.Lock()
    success_event = threading.Event()

    def submit_one(wallet):
        if success_event.is_set() or time.time() > end_time:
            return None
        try:
            bal = _get_wallet_balance(subtensor, wallet)
        except Exception:
            bal = None
        if bal is None:
            print(f"{wallet.name} balance unknown, attempting anyway.")
        elif _compare_balances(bal, burn_cost):
            print(f"{wallet.name} insufficient balance {bal} < {burn_cost}, skipping.")
            return None
        ok, info = _try_register(subtensor, wallet, NETUID)
        with submitted_lock:
            submitted.append((wallet.name, ok, info))
        if ok:
            print(f"Submitted for {wallet.name}: {info}")
        else:
            print(f"Submit error for {wallet.name}: {info}")
        return info

    # Prepare and broadcast in rapid succession (parallelized)
    max_workers = min(MAX_WORKERS, max(1, len(wallets)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for wallet in wallets:
            if time.time() > end_time:
                break
            futures.append(executor.submit(submit_one, wallet))

        # Monitor submitted txs / chain state for success until timeout or success
        while time.time() < end_time and not success_event.is_set():
            for w in wallets:
                try:
                    hot = w.hotkey.ss58_address
                except Exception:
                    continue
                try:
                    uid = _get_uid_for_hotkey(subtensor, hot, NETUID)
                except Exception:
                    uid = None
                if uid is not None and uid != 0:
                    with success_lock:
                        if not success_event.is_set():
                            success = (w.name, uid)
                            success_event.set()
                    break
            time.sleep(0.2)

        # Drain futures
        for _ in as_completed(futures):
            pass

    if success_event.is_set() and success:
        print("Registration succeeded for", success)
    else:
        print("No registration in the time window. Submitted results:")
        for name, ok, info in submitted:
            print(f"  - {name}: {'ok' if ok else 'err'} -> {info}")

if __name__ == "__main__":
    main()
