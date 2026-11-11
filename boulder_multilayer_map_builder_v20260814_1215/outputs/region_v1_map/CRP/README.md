# CRP dataset

This folder contains 32 non-duplicate trans-domain CRP cluster signatures.

Each record combines climate, terrain/infrastructure and UTC time-window indicators.
Each cluster now has the canonical runtime form `C_k=(mu_k, Sigma_k, n_k, q_k)`.
`crp_runtime_statistics.json` stores centroid, empirical covariance, sample_count, quality_score, feature_order, and a regularized covariance copy for stable runtime inversion.
`crp_cluster_catalog.csv` retains all previous columns and adds explicit `sample_count` and `quality_score` columns.
The cluster map contains one band per UTC time window. Cluster IDs use 0-based numbering.

The critical level is a project composite flag and must be recalibrated using the actual UAV envelope, 
clearance, battery-temperature limits and maximum allowable wind.
