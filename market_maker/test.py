from __future__ import absolute_import
from time import sleep
import sys
from datetime import *
import os
from os.path import getmtime
import random
import requests
import atexit
import signal
from operator import itemgetter
import numpy as np

from market_maker import bitmex
from market_maker.settings import settings
from market_maker.utils import log, constants, errors
from market_maker.utils.rest import ApiException
from market_maker.utils.trade_api import TradeApi
import talib as ta

# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]


#
# Helpers
#
logger = log.setup_custom_logger('root')

class HTTPTradeApi(object):
    def __init__(self, dry_run=False):
        self.last=""
        self.close=[]
        self.api = TradeApi()
    def trade_get_bucketed(self,start):
        trades = self.api.trade_get_bucketed(bin_size="1m",symbol="XBT",count=500,start_time=start)
        for trade in trades: 
            self.close.append(trade.close)
            self.last=str(trade.timestamp)

class ExchangeInterface:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        if len(sys.argv) > 1:
            self.symbol = sys.argv[1]
        else:
            self.symbol = settings.SYMBOL
        self.bitmex = bitmex.BitMEX(base_url=settings.BASE_URL, symbol=self.symbol, login=settings.LOGIN,
                                    password=settings.PASSWORD, otpToken=settings.OTPTOKEN, apiKey=settings.API_KEY,
                                    apiSecret=settings.API_SECRET, orderIDPrefix=settings.ORDERID_PREFIX)

    def cancel_order(self, order):
        logger.info("Cancelling: %s %d @ %.2f" % (order['side'], order['orderQty'], "@", order['price']))
        while True:
            try:
                self.bitmex.cancel(order['orderID'])
                sleep(settings.API_REST_INTERVAL)
            except ValueError as e:
                logger.info(e)
                sleep(settings.API_ERROR_INTERVAL)
            else:
                break

    def cancel_all_orders(self):
        if self.dry_run:
            return

        #logger.info("Resetting current position. Cancelling all existing orders.")

        # In certain cases, a WS update might not make it through before we call this.
        # For that reason, we grab via HTTP to ensure we grab them all.
        orders = self.bitmex.http_open_orders()

        for order in orders:
            logger.info("Cancelling: %s %d @ %.2f" % (order['side'], order['orderQty'], order['price']))

        if len(orders):
            self.bitmex.cancel([order['orderID'] for order in orders])

        sleep(settings.API_REST_INTERVAL)

    def get_portfolio(self):
        contracts = settings.CONTRACTS
        portfolio = {}
        for symbol in contracts:
            position = self.bitmex.position(symbol=symbol)
            instrument = self.bitmex.instrument(symbol=symbol)

            if instrument['isQuanto']:
                future_type = "Quanto"
            elif instrument['isInverse']:
                future_type = "Inverse"
            else:
                raise NotImplementedError("Unknown future type; not quanto or inverse: %s" % instrument.symbol)

            multiplier = float(instrument['multiplier']) / float(instrument['underlyingToSettleMultiplier'])

            portfolio[symbol] = {
                "currentQty": float(position['currentQty']),
                "futureType": future_type,
                "multiplier": multiplier,
                "markPrice": float(instrument['markPrice']),
                "spot": float(instrument['indicativeSettlePrice'])
            }

        return portfolio

    def calc_delta(self):
        """Calculate currency delta for portfolio"""
        portfolio = self.get_portfolio()
        spot_delta = 0
        mark_delta = 0
        for symbol in portfolio:
            item = portfolio[symbol]
            if item['futureType'] == "Quanto":
                spot_delta += item['currentQty'] * item['multiplier'] * item['spot']
                mark_delta += item['currentQty'] * item['multiplier'] * item['markPrice']
            elif item['futureType'] == "Inverse":
                spot_delta += (item['multiplier'] / item['spot']) * item['currentQty']
                mark_delta += (item['multiplier'] / item['markPrice']) * item['currentQty']
        basis_delta = mark_delta - spot_delta
        delta = {
            "spot": spot_delta,
            "mark_price": mark_delta,
            "basis": basis_delta
        }
        return delta
    def update_close_data(self) :
        """update close data every mins """ 
        close = self.bitmex.last_close()
        timestamp = close["timestamp"]
        if self.http.last != timestamp :
            if len(self.http.close) < 500:
                self.http.close += [close["close"]]
            else :
                self.http.close = self.http.close[1:]+[close["close"]]
        self.http.last = timestamp

    def get_delta(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.get_position(symbol)['currentQty']

    def get_instrument(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.instrument(symbol)

    def get_margin(self):
        if self.dry_run:
            return {'marginBalance': float(settings.DRY_BTC), 'availableFunds': float(settings.DRY_BTC)}
        return self.bitmex.funds()
    def recent_trades(self,symbol=settings.symbol):
        return self.bitmex.recent_trades(symbol)[-1]
    def get_orders(self):
        if self.dry_run:
            return []
        return self.bitmex.open_orders()
    def last_close(self):
        return self.bitmex.last_close()
    def get_highest_buy(self):
        buys = [o for o in self.get_orders() if o['side'] == 'Buy']
        if not len(buys):
            return {'price': -2**32, 'orderQty':0}
        highest_buy = max(buys or [], key=lambda o: o['price'])
        return highest_buy if highest_buy else {'price': -2**32}

    def get_lowest_sell(self):
        sells = [o for o in self.get_orders() if o['side'] == 'Sell']
        if not len(sells):
            return {'price': 2**32, 'orderQty':0}
        lowest_sell = min(sells or [], key=lambda o: o['price'])
        return lowest_sell if lowest_sell else {'price': 2**32}  # ought to be enough for anyone

    def get_position(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.position(symbol)

    def get_ticker(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.ticker_data(symbol)
    
    def market_depth(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.market_depth(symbol)

    def is_open(self):
        """Check that websockets are still open."""
        return not self.bitmex.ws.exited

    def check_market_open(self):
        instrument = self.get_instrument()
        if instrument["state"] != "Open":
            raise errors.MarketClosedError("The instrument %s is not open. State: %s" %
                                           (self.symbol, instrument["state"]))
            sys.exit()

    def check_if_orderbook_empty(self):
        """This function checks whether the order book is empty"""
        instrument = self.get_instrument()
        if instrument['midPrice'] is None:
            raise errors.MarketEmptyError("Orderbook is empty, cannot quote")
            sys.exit()

    def amend_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.amend_bulk_orders(orders)

    def create_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.create_bulk_orders(orders)

    def cancel_bulk_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.cancel([order['orderID'] for order in orders])


class OrderManager:
    def __init__(self):
        self.exchange = ExchangeInterface(settings.DRY_RUN)
        self.target_MA1 = settings.target_MA1 
        self.target_MA2 = settings.target_MA2
        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        atexit.register(self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        logger.info("Using symbol %s." % self.exchange.symbol)

    def init(self):
        if settings.DRY_RUN:
            logger.info("Initializing dry run. Orders printed below represent what would be posted to BitMEX.")
        else:
            logger.info("Order Manager initializing, connecting to BitMEX. Live run: executing real trades.")

        self.start_time = datetime.now()
        self.instrument = self.exchange.get_instrument()
        self.starting_qty = self.exchange.get_delta()
        self.running_qty = self.starting_qty 
        self.http = HTTPTradeApi()
        start = datetime.utcnow() - timedelta(minutes=500)
        self.http.trade_get_bucketed(start)
    def stop_profit(self) :
        position = self.exchange.get_position()
        recent_trade = self.exchange.recent_trades()
        if os.path.exists("./.stop_profit") :
            return "stopped",[]
	cost = position['avgCostPrice']
        if cost > recent_trade['price'] or cost <= 0:
            return 'no',[]
        if abs(float(cost)-float(recent_trade['price']))/float(cost) >= settings.stop_target and position >0:
            os.mkdir("./.stop_profit")
            return "stopping",[{'price':recent_trade['price'] , 'orderQty': self.running_qty, 'side': "Sell"}]   
        return "no",[]  
        #self.reset()
    def update_close_data(self) :
        """update close data every mins """
        close = self.exchange.last_close() 
        timestamp = close["timestamp"]
        if self.http.last != timestamp :
            if len(self.http.close) < 500:
                self.http.close += [close["close"]]
            else :
                self.http.close = self.http.close[1:]+[close["close"]]
        self.http.last = timestamp
    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()

        # Create orders and converge.
        self.place_orders()

        if settings.DRY_RUN:
            sys.exit()

    def print_status(self):
        """Print the current MM status."""

        margin = self.exchange.get_margin()
        position = self.exchange.get_position()
        self.running_qty = self.exchange.get_delta()
        self.start_XBt = margin["marginBalance"]

        logger.info("Current XBT Balance: %.6f" % XBt_to_XBT(self.start_XBt))
        logger.info("Current Contract Position: %d" % self.running_qty)
        if settings.CHECK_POSITION_LIMITS:
            logger.info("Position limits: %d/%d" % (settings.MIN_POSITION, settings.MAX_POSITION))
        if position['currentQty'] != 0:
            logger.info("Avg Cost Price: %.2f" % float(position['avgCostPrice']))
            logger.info("Avg Entry Price: %.2f" % float(position['avgEntryPrice']))
        logger.info("Contracts Traded This Run: %d" % (self.running_qty - self.starting_qty))
        logger.info("Total Contract Delta: %.4f XBT" % self.exchange.calc_delta()['spot'])

    def get_ticker(self):
        ticker = self.exchange.get_ticker()
        print self.exchange.market_depth()
        order_book = sorted(self.exchange.market_depth(), key=itemgetter('level'))
        highest_buy = self.exchange.get_highest_buy()
        lowest_sell = self.exchange.get_lowest_sell()

        # Set up our buy & sell positions as the smallest possible unit above and below the current spread
        # and we'll work out from there. That way we always have the best price but we don't kill wide
        # and potentially profitable spreads.
        buy_start = ticker["buy"]
        sell_start = ticker["sell"]
        
        # If we're maintaining spreads and we already have orders in place,
        # make sure they're not ours. If they are, we need to adjust, otherwise we'll
        # just work the orders inward until they collide.
        if settings.MAINTAIN_SPREADS:
            bid_levels = [x["bidSize"] for x in filter(lambda x: x["bidSize"]!=None, order_book)]
            bid_prices = [x["bidPrice"] for x in filter(lambda x: x["bidPrice"]!=None, order_book)]
            bid_index = next(x[0] for x in enumerate(np.cumsum(bid_levels)-highest_buy["orderQty"]) if x[1] > settings.MIN_CONTRACTS)
            buy_start = bid_prices[bid_index]
            print("Bid Prices: "+str(bid_prices))
            print("Bid Levels: "+str(bid_levels))
            print("Bid Index: "+str(bid_index))
            ask_levels = [x["askSize"] for x in filter(lambda x: x["askSize"]!=None, order_book)]
            ask_prices = [x["askPrice"] for x in filter(lambda x: x["askPrice"]!=None, order_book)]
            ask_index = next(x[0] for x in enumerate(np.cumsum(ask_levels)-lowest_sell["orderQty"]) if x[1] > settings.MIN_CONTRACTS)
            sell_start = ask_prices[ask_index]
            print("Ask Prices: "+str(ask_prices))
            print("Ask Levels: "+str(ask_levels))
            print("Ask Index: "+str(ask_index))
                
        self.start_position_buy = buy_start + self.instrument['tickSize']
        self.start_position_sell = sell_start - self.instrument['tickSize']

        # Back off if our spread is too small.
        if self.start_position_buy * (1.00 + settings.MIN_SPREAD) > self.start_position_sell:
            self.start_position_buy *= (1.00 - (settings.MIN_SPREAD / 2))
            self.start_position_sell *= (1.00 + (settings.MIN_SPREAD / 2))

        # Midpoint, used for simpler order placement.
        self.start_position_mid = ticker["mid"]
        logger.info(
            "%s Ticker: Buy: %.2f, Sell: %.2f" %
            (self.instrument['symbol'], ticker["buy"], ticker["sell"])
        )
        logger.info('Start Positions: Buy: %.2f, Sell: %.2f, Mid: %.2f' %
                    (self.start_position_buy, self.start_position_sell, self.start_position_mid))
        return ticker

    def get_price_offset(self, index):
        """Given an index (1, -1, 2, -2, etc.) return the price for that side of the book.
           Negative is a buy, positive is a sell."""
        # Maintain existing spreads for max profit
        if settings.MAINTAIN_SPREADS:
            start_position = self.start_position_buy if index < 0 else self.start_position_sell
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        else:
            # Offset mode: ticker comes from a reference exchange and we define an offset.
            start_position = self.start_position_buy if index < 0 else self.start_position_sell

            # If we're attempting to sell, but our sell price is actually lower than the buy,
            # move over to the sell side.
            if index > 0 and start_position < self.start_position_buy:
                start_position = self.start_position_sell
            # Same for buys.
            if index < 0 and start_position > self.start_position_sell:
                start_position = self.start_position_buy

        return round(start_position * (1 + settings.INTERVAL) ** index, self.instrument['tickLog'])

    ###
    # Orders
    ###
    def whatToDo(self):
        close = self.http.close
        recent_trade = self.exchange.recent_trades()
        ma1 = settings.MA1 if settings.MA1 < settings.MA2 else settings.MA2
        ma2 = settings.MA2 if settings.MA1 < settings.MA2 else settings.MA1
        MA1 = ta.MA(np.array(close+[recent_trade['price']]),ma1)[-1]
        MA2 = ta.MA(np.array(close+[recent_trade['price']]),ma2)[-1]
        recent_trade = self.exchange.recent_trades()
        logger.info("MA%s: %s,MA%s: %s ,last trade: %s" %(ma1,MA1,ma2,MA2,recent_trade['price'])) 
        if recent_trade['price'] < MA1 and MA1 < MA2 :
            logger.info("do short")
            try:
                os.rmdir("./.stop_profit")
            except:
                pass
            self.target_MA1 = False
            self.target_MA2 = False
            return -1
        elif recent_trade['price'] > MA1 and MA1 > MA2:
            logger.info("do long")
            self.target_MA1 = False
            self.target_MA2 = False
            return 1
        '''
        elif recent_trade['price'] < MA1 and position > 0 and self.target_MA1 == False:
            to_create = [{'price':recent_trade['price'] , 'orderQty': position/3, 'side': "Sell"}]
            logger.info("target profit")
            self.target_MA1 = True
        elif recent_trade['price'] < MA2 and position > 0 and self.target_MA2 == False:
            to_create = [{'price':recent_trade['price'] , 'orderQty': position/3, 'side': "Sell"}]
            logger.info("target profit")
            self.target_MA2 = True
        elif recent_trade['price'] >  MA1 and position < 0 and self.target_MA1 == False: 
            to_create = [{'price':recent_trade['price'] , 'orderQty': abs(position)/3, 'side': "Buy"}]
            logger.info("stop loss")
            self.target_MA1 = True
        elif recent_trade['price'] >  MA2 and position < 0 and self.target_MA2 == False:
            to_create = [{'price':recent_trade['price'] , 'orderQty': abs(position)/3, 'side': "Buy"}]
            logger.info("stop loss")
            self.target_MA2 = True
 
        return 0,to_create
        '''
        return 0

    def begin_orders(self) :
        to_create =[]
        to_amend= []
        to_cancel=[]
        position = self.exchange.get_delta()
        todo = self.whatToDo()
        to_create = []
        orders = existing_orders = self.exchange.get_orders()
        for order in orders :
            if order['side'] == 'Buy' and todo <0:
                to_cancel.append(order)
            elif order['side'] == 'Sell' and todo >0 :
                to_cancel.append(order)   
        ret,to_create=self.stop_profit()
        if ret == 'stopped' and position == 0:
            return 
        amount = 0
        if todo == -1 :
            if position < 0 and position > settings.MIN_POSITION :
                amount = abs(settings.MIN_POSITION)-position
            elif position >=0 :
                amount = position + abs(settings.MIN_POSITION)
        elif todo == 1 :
            if position >0 and position < settings.MAX_POSITION and ret != 'stopped':
                amount = settings.MAX_POSITION - position
            elif position <= 0 :
                amount = abs(position) + settings.MAX_POSITION
        if len(to_create) > 0 or amount > 0:
           price = self.exchange.recent_trades()['price']
           if amount > 0:
               create_order = {'price':price , 'orderQty': amount, 'side': "Buy" if todo > 0 else "Sell"}
               to_create.append(create_order)
           orders = existing_orders = self.exchange.get_orders() 
           for order in orders:
               if order['price'] == to_create[0]['price'] :
                   to_create.remove(to_create[0])
               else:
                   to_amend.append({'orderID': order['orderID'], 'leavesQty': to_create[0]['orderQty'],
                                     'price':  to_create[0]['price'], 'side': order['side']})
                   to_create.remove(to_create[0])
           if len(to_create) > 0: 
               logger.info("%4s %d @ %s" % (to_create[0]['side'], to_create[0]['orderQty'],  to_create[0]['price']))
               try:
                   self.exchange.create_bulk_orders(to_create) 
               except:
                   pass
        if len(to_cancel) > 0:
            logger.info("need cancel ...")
            try:
                self.exchange.cancel_bulk_orders(to_cancel)
            except:
                pass
        if len(to_amend) >0 :
            logger.info("need amend ...")
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except:
                pass      
    def place_orders(self):
        """Create order items for use in convergence."""

        buy_orders = []
        sell_orders = []
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        if self.enough_liquidity():
            for i in reversed(range(1, settings.ORDER_PAIRS + 1)):
                if not self.long_position_limit_exceeded():
                    buy_orders.append(self.prepare_order(-i))
                if not self.short_position_limit_exceeded():
                    sell_orders.append(self.prepare_order(i))

        return self.converge_orders(buy_orders, sell_orders)

    def prepare_order(self, index):
        """Create an order object."""

        if settings.RANDOM_ORDER_SIZE is True:
            quantity = random.randint(settings.MIN_ORDER_SIZE, settings.MAX_ORDER_SIZE)
        else:
            quantity = settings.ORDER_START_SIZE + ((abs(index) - 1) * settings.ORDER_STEP_SIZE)

        price = self.get_price_offset(index)

        return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def converge_orders(self, buy_orders, sell_orders):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            try:
                if order['side'] == 'Buy':
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                else:
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'leavesQty': desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)

        while buys_matched < len(buy_orders):
            to_create.append(buy_orders[buys_matched])
            buys_matched += 1

        while sells_matched < len(sell_orders):
            to_create.append(sell_orders[sells_matched])
            sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    amended_order['leavesQty'], tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                elif errorObj['error']['message'] == 'Order price is above the liquidation price of current short position':
                    logger.warn("Amending failed. no need to care.")
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_bulk_orders(to_cancel)

    ###
    # Position Limits
    ###

    def short_position_limit_exceeded(self):
        "Returns True if the short position limit is exceeded"
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position <= settings.MIN_POSITION

    def long_position_limit_exceeded(self):
        "Returns True if the long position limit is exceeded"
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position >= settings.MAX_POSITION

    ###
    # Liquidity
    ##
    def enough_liquidity(self):
        "Returns true if there is enough liquidity on each side of the order book"
        enough_liquidity = False
        ticker = self.exchange.get_ticker()
        order_book = self.exchange.market_depth()
        highest_buy = self.exchange.get_highest_buy()
        lowest_sell = self.exchange.get_lowest_sell()

        bid_depth = sum([x["bidSize"] for x in filter(lambda x: x["bidSize"]!=None, order_book)])
        ask_depth = sum([x["askSize"] for x in filter(lambda x: x["askSize"]!=None, order_book)])

        bid_liquid = bid_depth - highest_buy["orderQty"]
        logger.info("Bid Liquidity: "+str(bid_liquid)+" Contracts")
        ask_liquid = ask_depth - lowest_sell["orderQty"]
        logger.info("Ask Liquidity: "+str(ask_liquid)+" Contracts")
        enough_ask_liquidity = ask_liquid >= settings.MIN_CONTRACTS
        enough_bid_liquidity = bid_liquid >= settings.MIN_CONTRACTS
        enough_liquidity = (enough_ask_liquidity and enough_bid_liquidity)
        if not enough_liquidity:
            if (not enough_bid_liquidity) and (not enough_ask_liquidity):
                logger.info("Neither side has enough liquidity")
            elif not enough_bid_liquidity:
                logger.info("Bid side is not liquid enough")
            else:
                logger.info("Ask side is not liquid enough")
        return enough_liquidity
    
    ###
    # Sanity
    ##

    def sanity_check(self):
        """Perform checks before placing orders."""

        # Check if OB is empty - if so, can't quote.
        self.exchange.check_if_orderbook_empty()

        # Ensure market is still open.
        self.exchange.check_market_open()

        # Get ticker, which sets price offsets and prints some debugging info.
        ticker = self.get_ticker()

        # Sanity check:
        if self.get_price_offset(-1) >= ticker["sell"] or self.get_price_offset(1) <= ticker["buy"]:
            logger.error(self.start_position_buy, self.start_position_sell)
            logger.error("%s %s %s %s" % (self.get_price_offset(-1), ticker["sell"], self.get_price_offset(1), ticker["buy"]))
            logger.error("Sanity check failed, exchange data is inconsistent")
            sys.exit()

        # Messanging if the position limits are reached
        if self.long_position_limit_exceeded():
            logger.info("Long delta limit exceeded")
            logger.info("Current Position: %.f, Maximum Position: %.f" %
                        (self.exchange.get_delta(), settings.MAX_POSITION))

        if self.short_position_limit_exceeded():
            logger.info("Short delta limit exceeded")
            logger.info("Current Position: %.f, Minimum Position: %.f" %
                        (self.exchange.get_delta(), settings.MIN_POSITION))

    ###
    # Running
    ###

    def check_file_change(self):
        """Restart if any files we're watching have changed."""
        for f, mtime in watched_files_mtimes:
            if getmtime(f) > mtime:
                self.restart()

    def check_connection(self):
        """Ensure the WS connections are still open."""
        return self.exchange.is_open()

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        try:
            #self.exchange.cancel_all_orders()
            self.exchange.bitmex.ws.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

    def run_loop(self):
        while True:
            sys.stdout.write("-----\n")
            sys.stdout.flush()
            
            self.check_file_change()
            sleep(settings.LOOP_INTERVAL)
            #self.update_close_data()
            #self.print_status() 
            #self.begin_orders() 
            
            # This will restart on very short downtime, but if it's longer,
            # the MM will crash entirely as it is unable to connect to the WS on boot.
            if not self.check_connection():
                logger.error("Realtime data connection unexpectedly closed, restarting.")
                self.restart()
             
            self.sanity_check()  # Ensures health of mm - several cut-out points here
            self.print_status()  # Print skew, delta, etc
            self.place_orders()  # Creates desired orders and converges to existing orders
            
    def restart(self):
        logger.info("Restarting the market maker...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

#
# Helpers
#
def XBt_to_XBT(XBt):
    return float(XBt) / constants.XBt_TO_XBT


def cost(instrument, quantity, price):
    mult = instrument["multiplier"]
    P = mult * price if mult >= 0 else mult / price
    return abs(quantity * P)

def MA(close,margin,period):
    return ta.MA(np.array(close+[margin]),period)[-1]
def margin(instrument, quantity, price):
    return cost(instrument, quantity, price) * instrument["initMargin"]


def run():
    logger.info('BitMEX Market Maker Version: %s\n' % constants.VERSION)

    om = OrderManager()
    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        om.init()
        om.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()

