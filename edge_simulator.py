import socket
import time

def send_cmd(cmd: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # The listener in engine.py is on port 5555
    sock.sendto(cmd.encode("utf-8"), ("127.0.0.1", 5555))
    sock.close()

def main():
    print("========================================")
    print("👽 EDGE SIMULATOR - Target <=> Lighter")
    print("========================================")
    print("Instrucciones:")
    print("A - Simular Oportunidad Mínima de APERTURA (Inyecta spread irreal)")
    print("C - Simular Oportunidad Mínima de CIERRE (Inyecta spread irreal)")
    print("Q - Salir")
    print("----------------------------------------")
    
    while True:
        try:
            op = input("\nIngresa comando (A / C / Q): ").strip().upper()
            if op == "A":
                print(">> 🚀 Inyectando Falso Edge de APERTURA durante 1 segundo...")
                send_cmd("OPEN")
            elif op == "C":
                print(">> 🔻 Inyectando Falso Edge de CIERRE durante 1 segundo...")
                send_cmd("CLOSE")
            elif op in ["Q", "QUIT", "EXIT"]:
                print("Bye!")
                break
            else:
                print("Comando inválido.")
        except KeyboardInterrupt:
            print("\nBye!")
            break

if __name__ == "__main__":
    main()
