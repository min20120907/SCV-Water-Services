"""
Prophet Prediction Service for SCV Water Dashboard
Replaces Gemini-based curve prediction with proper time-series forecasting.

Endpoints:
  POST /predict          → Prophet forecast from historical readings
  GET  /health           → liveness probe
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from prophet import Prophet
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prophet-service")

app = FastAPI(title="SCV Water Prophet Service", version="1.0.0")

# CORS — allow Flutter web dashboard to call this service
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to dashboard origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class PredictRequest(BaseModel):
    device_id: str
    readings: list[dict]          # [{"timestamp": epoch_ms, "value": float}, ...]
    horizon_hours: int = 24       # how many hours to predict
    interval_minutes: int = 60    # bucket size for aggregation


class PredictResponse(BaseModel):
    device_id: str
    forecast: list[dict]          # [{"timestamp": epoch_ms, "yhat": float, "yhat_lower": float, "yhat_upper": float}, ...]
    model_info: dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _readings_to_df(readings: list[dict], interval_minutes: int) -> pd.DataFrame:
    """Convert raw readings to a Prophet-compatible DataFrame (ds, y)."""
    if not readings:
        raise ValueError("No readings provided")

    rows = []
    for r in readings:
        ts = r.get("timestamp")
        val = r.get("value")
        if ts is None or val is None:
            continue
        # epoch ms → datetime
        dt = datetime.fromtimestamp(ts / 1000.0)
        rows.append({"ds": dt, "y": float(val)})

    if not rows:
        raise ValueError("No valid readings after filtering")

    df = pd.DataFrame(rows)
    df = df.sort_values("ds").reset_index(drop=True)

    # Aggregate into buckets (mean)
    bucket_str = f"{interval_minutes}min"
    df = df.set_index("ds").resample(bucket_str).mean().dropna().reset_index()

    if len(df) < 2:
        raise ValueError(f"Need at least 2 data points after aggregation, got {len(df)}")

    return df


def _fit_prophet(df: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    """Fit Prophet model and generate forecast."""
    # Suppress Prophet's verbose output
    import logging
    logging.getLogger("prophet").setLevel(logging.ERROR)
    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)

    model = Prophet(
        daily_seasonality=True,
        weekly_seasonality=True,
        yearly_seasonality=False,       # not enough data for yearly
        changepoint_prior_scale=0.05,   # conservative trend changes
        seasonality_prior_scale=10.0,
        interval_width=0.95,
    )

    model.fit(df)

    future = model.make_future_dataframe(periods=horizon_hours, freq="h")
    forecast = model.predict(future)

    return forecast


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/predict", response_model=PredictResponse)
async def predict(req: PredictRequest):
    logger.info(
        f"Predict request: device={req.device_id}, "
        f"readings={len(req.readings)}, horizon={req.horizon_hours}h"
    )

    try:
        df = _readings_to_df(req.readings, req.interval_minutes)
        logger.info(f"Aggregated to {len(df)} buckets (interval={req.interval_minutes}min)")

        forecast = _fit_prophet(df, req.horizon_hours)

        # Extract only the future rows (last horizon_hours rows)
        future_rows = forecast.tail(req.horizon_hours)

        result = []
        for _, row in future_rows.iterrows():
            result.append({
                "timestamp": int(row["ds"].timestamp() * 1000),
                "yhat": round(float(row["yhat"]), 2),
                "yhat_lower": round(float(row["yhat_lower"]), 2),
                "yhat_upper": round(float(row["yhat_upper"]), 2),
            })

        model_info = {
            "training_points": len(df),
            "interval_minutes": req.interval_minutes,
            "horizon_hours": req.horizon_hours,
            "last_training_ts": int(df["ds"].iloc[-1].timestamp() * 1000),
        }

        return PredictResponse(
            device_id=req.device_id,
            forecast=result,
            model_info=model_info,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Prediction failed")
        raise HTTPException(status_code=500, detail=f"Prediction error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)
