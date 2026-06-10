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
 * \page   RigidBodyCollection.cpp
 * \file   RigidBodyCollection.cpp
 * \brief  RigidBody Collection Implementation
 *********************************************************************/

#include "RigidBodyCollection.h"

//////////////////////////////////////////////////////////////////////////
// RigidBodyCollection implementation
//////////////////////////////////////////////////////////////////////////


RigidBodyCollection::RigidBodyCollection()
  :mNumRigidBodies(0)
{
  ;
}

/**
 * Append Rigid Body data to Rigid Body Collection.
 * 
 * \param rigidBodyData
 * \param numRigidBodies
 */
void RigidBodyCollection::AppendRigidBodyData(sRigidBodyData const * const rigidBodyData, size_t numRigidBodies)
{
    for (size_t i = 0; i < numRigidBodies; ++i)
    {
      const sRigidBodyData& rb = rigidBodyData[i];
      mXYZCoord[i + mNumRigidBodies] = std::make_tuple(rb.x, rb.y, rb.z);

      mXYZWQuats[i + mNumRigidBodies] = std::make_tuple(rb.qx, rb.qy, rb.qz, rb.qw);
      mIds[i + mNumRigidBodies] = rb.ID;
    }
    mNumRigidBodies += numRigidBodies;
}
