import ccxt
import os
from dotenv import load_dotenv

class BinanceClient:
    def __init__(self):
        load_dotenv(override=True)
        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_SECRET")
        
        if not api_key or not secret:
            raise ValueError("❌ Faltan las API Keys de BINANCE en el .env")

        self.client = ccxt.binance({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        # Cargar mercados para asegurar que el símbolo existe
        self.client.load_markets()
        print("✅ Cliente Binance Conectado")

    def get_price_ticker(self, symbol):
        """Devuelve el precio Bid (Venta rápida) y Ask (Compra rápida)"""
        try:
            ticker = self.client.fetch_ticker(symbol)
            return {
                'bid': float(ticker['bid']), # Precio al que puedes VENDER (Market Sell)
                'ask': float(ticker['ask'])  # Precio al que puedes COMPRAR (Market Buy)
            }
        except Exception as e:
            print(f"⚠️ Error Binance Ticker: {e}")
            return None

    def get_balance(self, coin):
        """Devuelve el saldo libre de una moneda"""
        try:
            bal = self.client.fetch_balance()
            return float(bal.get(coin, {}).get('free', 0.0))
        except Exception as e:
            print(f"⚠️ Error Binance Balance: {e}")
            return 0.0

    def create_market_order(self, symbol, side, amount):
        """Ejecuta una orden a mercado"""
        try:
            # side debe ser 'buy' o 'sell'
            order = self.client.create_order(symbol, 'market', side, amount)
            print(f"⚡ Orden Binance {side.upper()} ejecutada: {amount} {symbol}")
            return order
        except Exception as e:
            print(f"❌ Error Orden Binance: {e}")
            return None
