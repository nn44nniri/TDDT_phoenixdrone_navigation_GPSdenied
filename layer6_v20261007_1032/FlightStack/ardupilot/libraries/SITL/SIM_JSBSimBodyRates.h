/*
 * Native JSBSim body-rate side channel, R6.04 v1 (AI-assisted repair).
 * The FlightGear Euler-rate fields remain unmodified. Their float precision
 * cannot represent body rates reliably at the tailsitter Euler singularity.
 */
#pragma once

#include <cmath>
#include <cfloat>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace SITL {

struct JSBSimBodyRates {
    double time_s;
    float p;
    float q;
    float r;

    static bool header_matches(const char *text)
    {
        return strcmp(text, "<LABELS> ,Time,TDDT_BODY_RATES_V1_P_RAD_S,TDDT_BODY_RATES_V1_Q_RAD_S,TDDT_BODY_RATES_V1_R_RAD_S\n") == 0;
    }

    // JSBSim SOCKET output: Time, native body p/q/r (rad/s); versioned header.
    // No clipping, interpolation, zero-fill or fallback to Euler derivatives.
    static bool decode(const char *text, JSBSimBodyRates &sample)
    {
        double time, p, q, r;
        char extra;
        const int count = sscanf(text, "%lf,%lf,%lf,%lf %c", &time, &p, &q, &r, &extra);
        if (count != 4 || !std::isfinite(time) || time < 0 ||
            !std::isfinite(time * 1.0e6) || !std::isfinite(p) || !std::isfinite(q) || !std::isfinite(r) ||
            fabs(p) > FLT_MAX || fabs(q) > FLT_MAX || fabs(r) > FLT_MAX) {
            return false;
        }
        sample = {time, float(p), float(q), float(r)};
        return true;
    }

    uint32_t frame_time_us() const
    {
        // Match the FlightGear uint32 microsecond tag, including its wrap.
        return uint32_t(fmod(time_s * 1.0e6, 4294967296.0));
    }
};

} // namespace SITL
