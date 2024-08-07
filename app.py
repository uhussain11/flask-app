import json
import boto3
import yfinance as yf
import pandas as pd
import io
import importlib.util
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "http://localhost:3000"}}, supports_credentials=True)
logging.basicConfig(level=logging.INFO)

region = 'us-east-1'
s3_client = boto3.client('s3', region_name=region)
dynamodb_client = boto3.client('dynamodb', region_name=region)
bucket_name = 'finera'
result_table = 'BacktestResults'

def save_to_s3(df, key):
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer)
    s3_client.put_object(Bucket=bucket_name, Key=key, Body=csv_buffer.getvalue())

def load_from_s3(key):
    response = s3_client.get_object(Bucket=bucket_name, Key=key)
    csv_buffer = io.StringIO(response['Body'].read().decode('utf-8'))
    df = pd.read_csv(csv_buffer, index_col=0, parse_dates=True)
    return df

def fetch_data(ticker, start_date, end_date):
    df = yf.download(ticker, start=start_date, end=end_date, progress=False)
    return df

def create_and_execute_strategy(strategy_code, df, initial_capital):
    try:
        strategy_code_lines = strategy_code.strip().split('\n')
        strategy_code = '\n'.join([line.rstrip() for line in strategy_code_lines])

        strategy_code_with_imports = f"""
from backtesting import Strategy
from backtesting.lib import crossover
from backtesting.test import SMA

{strategy_code}
        """

        strategy_file = '/tmp/user_strategy.py'
        with open(strategy_file, 'w') as f:
            f.write(strategy_code_with_imports)

        logging.info(f"User strategy code written to {strategy_file}:\n{strategy_code_with_imports}")

        spec = importlib.util.spec_from_file_location("UserStrategy", strategy_file)
        user_strategy_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(user_strategy_module)

        UserStrategy = getattr(user_strategy_module, 'UserStrategy', None)
        if UserStrategy is None:
            logging.error("UserStrategy class not found in the user strategy module")
            raise AttributeError("UserStrategy class not found in the user strategy module")

        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        bt = Backtest(df, UserStrategy, cash=initial_capital, commission=.002)
        stats = bt.run()

        results = {
            'returns': stats['Return [%]'],
            'beta': stats.get('Beta', 'N/A'),
            'sharpe': stats['Sharpe Ratio'],
            'drawdown': stats['Max. Drawdown [%]'],
            'portfolioValue': stats['_equity_curve']['Equity'].tolist(),
            'benchmark': df['Close'].tolist()
        }

        return results
    except Exception as e:
        logging.error(f"Error in create_and_execute_strategy: {e}", exc_info=True)
        raise

@app.route('/fetch_data', methods=['POST'])
def fetch_data_handler():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No input data provided'}), 400

        ticker = data.get('ticker')
        period = data.get('period')
        
        if not ticker or not period:
            return jsonify({'error': 'Missing required parameters'}), 400

        start_date, end_date = period.split(':')
        cache_key = f"{ticker}_{start_date}_{end_date}.csv"

        try:
            df = load_from_s3(cache_key)
        except s3_client.exceptions.NoSuchKey:
            df = fetch_data(ticker, start_date, end_date)
            save_to_s3(df, cache_key)

        return jsonify({'message': 'Data fetched and cached', 'ticker': ticker, 'period': period, 'cache_key': cache_key})
    except Exception as e:
        logging.error(f"Error in fetch_data_handler: {e}")
        return jsonify({'error': 'Failed to fetch data'}), 500

@app.route('/run_backtest', methods=['POST'])
def run_backtest_handler():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No input data provided'}), 400

        ticker = data.get('ticker')
        period = data.get('period')
        cache_key = data.get('cache_key')
        capital = data.get('capital')
        strategy_code = data.get('strategy_code')

        if not ticker or not period or not cache_key or not capital or not strategy_code:
            return jsonify({'error': 'Missing required parameters'}), 400

        response = s3_client.get_object(Bucket=bucket_name, Key=cache_key)
        csv_buffer = io.StringIO(response['Body'].read().decode('utf-8'))
        df = pd.read_csv(csv_buffer, index_col=0, parse_dates=True)

        results = create_and_execute_strategy(strategy_code, df, float(capital))
        
        return jsonify({'message': 'Backtest completed', 'results': results, 'ticker': ticker, 'period': period, 'capital': capital})
    except Exception as e:
        logging.error(f"Error in run_backtest_handler: {e}")
        return jsonify({'error': 'Failed to run backtest'}), 500

@app.route('/store_results', methods=['POST'])
def store_results_handler():
    try:
        body = request.get_json()
        if not body:
            return jsonify({'error': 'No input data provided'}), 400

        ticker = body.get('ticker')
        period = body.get('period')
        capital = body.get('capital')
        results = body.get('results')

        if not ticker or not period or not capital or not results:
            return jsonify({'error': 'Missing required parameters'}), 400

        start_date, end_date = period.split(':')
        result_item = {
            'Ticker': {'S': ticker},
            'Period': {'S': period},
            'StartDate': {'S': start_date},
            'EndDate': {'S': end_date},
            'Capital': {'N': str(capital)},
            'Results': {'S': json.dumps(results)}
        }
        dynamodb_client.put_item(TableName=result_table, Item=result_item)

        return jsonify({'message': 'Results stored in DynamoDB'})
    except Exception as e:
        logging.error(f"Error in store_results_handler: {e}")
        return jsonify({'error': 'Failed to store results'}), 500

@app.route('/display_results', methods=['POST'])
def display_results_handler():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No input data provided'}), 400

        ticker = data.get('ticker')
        period = data.get('period')
        
        if not ticker or not period:
            return jsonify({'error': 'Missing required parameters'}), 400

        key = {
            'Ticker': {'S': ticker},
            'Period': {'S': period}
        }

        response = dynamodb_client.get_item(TableName=result_table, Key=key)
        
        if 'Item' in response:
            item = {k: list(v.values())[0] for k, v in response['Item'].items()}
            return jsonify(item)
        else:
            return jsonify({'error': 'Item not found'}), 404
    except Exception as e:
        logging.error(f"Error in display_results_handler: {e}")
        return jsonify({'error': 'Failed to display results'}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=True)
