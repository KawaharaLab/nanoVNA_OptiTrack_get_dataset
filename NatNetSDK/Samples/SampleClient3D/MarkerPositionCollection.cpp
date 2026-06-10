//=============================================================================
// Copyright © 2025 NaturalPoint, Inc. All Rights Reserved.
// 
// THIS SOFTWARE IS GOVERNED BY THE OPTITRACK PLUGINS EULA AVAILABLE AT https://www.optitrack.com/about/legal/eula.html 
// AND/OR FOR DOWNLOAD WITH THE APPLICABLE SOFTWARE FILE(S) (“PLUGINS EULA”). BY DOWNLOADING, INSTALLING, ACTIVATING 
// AND/OR OTHERWISE USING THE SOFTWARE, YOU ARE AGREEING THAT YOU HAVE READ, AND THAT YOU AGREE TO COMPLY WITH AND ARE
// BOUND BY, THE PLUGINS EULA AND ALL APPLICABLE LAWS AND REGULATIONS. IF YOU DO NOT AGREE TO BE BOUND BY THE PLUGINS
// EULA, THEN YOU MAY NOT DOWNLOAD, INSTALL, ACTIVATE OR OTHERWISE USE THE SOFTWARE AND YOU MUST PROMPTLY DELETE OR
// RETURN IT. IF YOU ARE DOWNLOADING, INSTALLING, ACTIVATING AND/OR OTHERWISE USING THE SOFTWARE ON BEHALF OF AN ENTITY,
// THEN BY DOING SO YOU REPRESENT AND WARRANT THAT YOU HAVE THE APPROPRIATE AUTHORITY TO ACCEPT THE PLUGINS EULA ON
// BEHALF OF SUCH ENTITY. See license file in root directory for additional governing terms and information.
//=============================================================================

/*********************************************************************
 * \page   MarkerPositionCollection.cpp
 * \file   MarkerPositionCollection.cpp
 * \brief  
 *********************************************************************/

#include "MarkerPositionCollection.h"

//////////////////////////////////////////////////////////////////////////
// MarkerPositionCollection implementation
//////////////////////////////////////////////////////////////////////////

MarkerPositionCollection::MarkerPositionCollection()
  :mMarkerPositionCount(0)
{
  ;
}

//////////////////////////////////////////////////////////////////////////
/// <summary>Appends the given marker positions to any existing marker positions
/// contained in self.
/// </summary>
/// <param name='markerData'>Array of marker x, y, z coordinates.<param>
/// <param name='numRigidBodies'>Number of markers.</param>
/// <remarks>The order of the marker positions is preserved.</remarks>
//////////////////////////////////////////////////////////////////////////
void MarkerPositionCollection::AppendMarkerPositions(float markerData[][3], size_t numMarkers)
{
  for (size_t i = 0; i < numMarkers; ++i)
  {
    mMarkerPositions[i + mMarkerPositionCount] = std::make_tuple(markerData[i][0], markerData[i][1], markerData[i][2]);
  }

  mMarkerPositionCount += numMarkers;
}

//////////////////////////////////////////////////////////////////////////
/// <summary>
/// Appends labeled marker data to any existing labeled marker data.
/// </summary>
/// <param name='markers'>Array of labeled marker data structures.</param>
/// <param name='numMarkers'>Number of labeled markers.</param>
//////////////////////////////////////////////////////////////////////////
void MarkerPositionCollection::AppendLabledMarkers(sMarker markers[], size_t numMarkers)
{
  for (size_t i = 0; i < numMarkers; ++i)
  {
    mLabledMarkers[i + mLabledMarkerCount].ID   = markers[i].ID;
    mLabledMarkers[i + mLabledMarkerCount].x    = markers[i].x;
    mLabledMarkers[i + mLabledMarkerCount].y    = markers[i].y;
    mLabledMarkers[i + mLabledMarkerCount].z    = markers[i].z;
    mLabledMarkers[i + mLabledMarkerCount].size = markers[i].size;
	mLabledMarkers[i + mLabledMarkerCount].params = markers[i].params;
  }

  mLabledMarkerCount += numMarkers;
}